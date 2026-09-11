from datetime import datetime

from module.base.decorator import cached_property
from module.base.timer import Timer
from module.combat.assets import BATTLE_STATUS_A, BATTLE_STATUS_B, BATTLE_STATUS_S, EXP_INFO_D, OPTS_INFO_D
from module.combat.emotion import Emotion, FleetEmotion
from module.exception import ScriptEnd
from module.logger import logger
from module.raid.assets import RAID_RETRY
from module.raid.daily import STAGE_FILTER, RaidStage
from module.raid.run import RaidRun
from module.ui.page import page_campaign_menu, page_raid

STAGES = ['hard', 'normal', 'easy']
# Oil cost varies in stages, use a conservative value to estimate oil when retrying
RETRY_OIL_COST = 60


class RaidLoseFleetEmotion(FleetEmotion):
    """
    Each raid stage has its own fleet, emotion is recorded in group `RaidLoseEmotion`
    """

    def __init__(self, config, stage):
        super().__init__(config, fleet=STAGES.index(stage) + 1)
        self.stage = stage
        self.prefix = f'RaidLoseEmotion_{stage.capitalize()}'

    @property
    def value(self):
        return getattr(self.config, f'{self.prefix}Value')

    @property
    def value_name(self):
        return f'{self.prefix}Value'

    @property
    def record(self):
        return getattr(self.config, f'{self.prefix}Record')

    @property
    def recover(self):
        return getattr(self.config, f'{self.prefix}Recover')

    @property
    def control(self):
        return getattr(self.config, f'{self.prefix}Control')

    @property
    def oath(self):
        return getattr(self.config, f'{self.prefix}Oath')


class RaidLoseEmotion(Emotion):
    def __init__(self, config):
        self.config = config
        self.fleets = [RaidLoseFleetEmotion(config, stage) for stage in STAGES]

    @property
    def is_calculate(self):
        return True

    @property
    def is_ignore(self):
        return False

    def show(self):
        for fleet in self.fleets:
            logger.attr(f'Emotion {fleet.stage}', fleet.value)

    def check_reduce(self, battle):
        # Stage is selected by emotion in RaidLose.select_stage()
        pass

    def stage_recovered(self, stage):
        """
        Returns:
            datetime.datetime: When will emotion of this stage >= control limit after a battle.
        """
        self.update()
        self.record()
        fleet = self.fleets[STAGES.index(stage)]
        return fleet.get_recovered(expected_reduce=self.reduce_per_battle)


class RaidLose(RaidRun):
    """
    Lose raid on purpose to farm affinity.
    User should set up fleets that are sure to be defeated, one fleet for each stage.
    Emotion is calculated as normal combats, stages are tried in order of RaidLose_StageFilter.

    RAID_RETRY only appears on the defeat result page (together with EXP_INFO_D).
    Click it to start next battle directly if current stage is still the one to run,
    otherwise click confirm and back to page_raid to select stage again.
    """
    won = False
    stage = 'hard'
    stages = STAGES
    remains = {}
    oil = 0
    # Decision of this battle, None for not decided yet
    retry_decision = None
    # If clicked RAID_RETRY and next battle is loading
    retried = False

    @cached_property
    def emotion(self):
        return RaidLoseEmotion(config=self.config)

    @cached_property
    def retry_wait_timer(self):
        # RAID_RETRY may appear later than EXP_INFO_D
        return Timer(3, count=6)

    def get_oil(self, *args, **kwargs):
        self.oil = super().get_oil(*args, **kwargs)
        return self.oil

    def combat_preparation(self, balance_hp=False, emotion_reduce=False, auto='combat_auto', fleet_index=1):
        super().combat_preparation(balance_hp=balance_hp, emotion_reduce=emotion_reduce, auto=auto,
                                   fleet_index=STAGES.index(self.stage) + 1)

    def handle_battle_status(self, drop=None):
        if not self.won and not self.is_combat_executing():
            for button in [BATTLE_STATUS_S, BATTLE_STATUS_A, BATTLE_STATUS_B]:
                if self.appear(button):
                    logger.warning(f'Raid won unexpectedly: {button}')
                    self.won = True
                    break
        return super().handle_battle_status(drop=drop)

    def should_retry(self):
        """
        Returns:
            bool: If run current stage again by RAID_RETRY.
                Emotion of current battle is already reduced in combat_preparation().
        """
        if self.won:
            return False
        if self.config.StopCondition_RunCount == 1:
            logger.info('Retry: no, this is the last run')
            return False
        if self.oil - RETRY_OIL_COST < max(500, self.config.StopCondition_OilLimit):
            logger.info(f'Retry: no, estimated oil {self.oil} is low, check it in page_campaign_menu')
            return False
        # Higher priority stage recovered
        for stage in self.stages:
            if stage == self.stage:
                break
            if self.remains.get(stage, 0) > 0 and self.emotion.stage_recovered(stage) <= datetime.now():
                logger.info(f'Retry: no, higher priority stage {stage} is available')
                return False
        if self.emotion.stage_recovered(self.stage) > datetime.now():
            logger.info(f'Retry: no, stage {self.stage} needs emotion recover')
            return False
        if self.config.task_switched():
            logger.info('Retry: no, task switched')
            return False

        logger.info(f'Retry: yes, stage {self.stage}')
        return True

    def handle_exp_info(self):
        if super().handle_exp_info():
            return True
        if self.is_combat_executing():
            return False
        if self.appear(EXP_INFO_D):
            if self.retry_decision is None:
                self.retry_decision = self.should_retry()
                self.retry_wait_timer.reset()
            # Wait RAID_RETRY, it will be clicked in raid_expected_end()
            if self.retry_decision and not self.retry_wait_timer.reached():
                return False
            if self.retry_decision:
                logger.warning('Wait RAID_RETRY timeout, confirm and back to raid page')
                self.retry_decision = False
            self.device.click(EXP_INFO_D)
            self.interval_reset(EXP_INFO_D)
            self.device.sleep((0.25, 0.5))
            return True

        return False

    def raid_retry(self):
        """
        Click RAID_RETRY until it disappears.
        RAID_RETRY is not clickable during its appear animation, so wait until it's stable.

        Returns:
            bool: If next battle is loading.

        Pages:
            in: defeat result page with RAID_RETRY
            out: battle loading, or defeat result page if failed
        """
        logger.info('Raid retry')
        stable_timer = Timer(1.5, count=3).start()
        click_timer = Timer(2)
        clicked = 0
        disappear = 0
        for _ in self.loop():
            if self.appear(RAID_RETRY, offset=(20, 20)):
                disappear = 0
                if not stable_timer.reached():
                    continue
                if click_timer.reached():
                    if clicked >= 5:
                        logger.warning('Failed to click RAID_RETRY after 5 trials')
                        return False
                    logger.info(f'{RAID_RETRY} -> retry')
                    self.device.click(RAID_RETRY)
                    clicked += 1
                    click_timer.reset()
                continue

            # End
            disappear += 1
            if disappear >= 2:
                if clicked:
                    logger.info('RAID_RETRY clicked, next battle is loading')
                    return True
                else:
                    logger.warning('RAID_RETRY disappeared before clicking')
                    return False

    def raid_expected_end(self):
        # Retry button on defeat result page
        if self.retry_decision and self.appear(RAID_RETRY, offset=(20, 20)):
            if self.raid_retry():
                self.retried = True
                self.oil -= RETRY_OIL_COST
                return True
            # Failed to retry, confirm and back to raid page
            self.retry_decision = False
            return False
        # Defeat tips page after D rank
        if self.appear_then_click(OPTS_INFO_D, offset=(30, 30), interval=3):
            return False
        return super().raid_expected_end()

    def select_stage(self):
        """
        Returns:
            str: First stage that has remain and enough emotion, or None if task delayed.

        Pages:
            in: page_raid
        """
        STAGE_FILTER.load(self.config.RaidLose_StageFilter)
        self.stages = [stage.name for stage in STAGE_FILTER.apply([RaidStage(stage) for stage in STAGES])]
        self.emotion.show()

        # Avoid ticket popup when free attempts run out
        self.remains = {}
        for stage in self.stages:
            self.remains[stage] = self.get_remain(stage)

        waits = []
        for stage in self.stages:
            if self.remains[stage] <= 0:
                logger.info(f'Raid {stage} has no remain')
                continue
            recovered = self.emotion.stage_recovered(stage)
            if recovered > datetime.now():
                logger.info(f'Raid {stage} emotion will recover at {recovered}')
                waits.append(recovered)
                continue
            return stage

        if waits:
            logger.info('All raid stages need emotion recover, delay task')
            self.config.task_delay(target=min(waits))
        else:
            logger.info('All raid stages have no remain, delay to server update')
            self.config.task_delay(server_update=True)
        return None

    def run(self, name='', mode='', total=0):
        name = name if name else self.config.Campaign_Event
        if self.is_raid_rpg():
            logger.info('RPG raid is not supported in RaidLose')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        self.run_count = 0
        self.run_limit = self.config.StopCondition_RunCount
        while 1:
            if not self.retried:
                if self.event_time_limit_triggered():
                    self.config.task_stop()

                # UI switches
                if not self._raid_has_oil_icon:
                    self.ui_ensure(page_campaign_menu)
                    if self.triggered_stop_condition(oil_check=True, coin_check=True):
                        break

                # UI ensure
                self.device.stuck_record_clear()
                self.device.click_record_clear()
                self.ui_ensure(page_raid)
                self.disable_event_on_raid()

                stage = self.select_stage()
                if stage is None:
                    break
                self.stage = stage

            # Log
            logger.hr(f'{name}_{self.stage}' + (' (retry)' if self.retried else ''), level=2)
            if self.config.StopCondition_RunCount > 0:
                logger.info(f'Count remain: {self.config.StopCondition_RunCount}')
            else:
                logger.info(f'Count: {self.run_count}')

            # Run
            self.won = False
            self.retry_decision = None
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                if self.retried:
                    # Already clicked RAID_RETRY, next battle is loading
                    self.retried = False
                    self.combat(balance_hp=False, expected_end=self.raid_expected_end)
                else:
                    self.raid_execute_once(mode=self.stage, raid=name)
            except ScriptEnd as e:
                logger.hr('Script end')
                logger.info(str(e))
                break

            if self.won:
                logger.critical('Fleet is supposed to be defeated in RaidLose, '
                                'task disabled, please check your fleet')
                self.config.Scheduler_Enable = False
                self.config.task_stop()

            # After run
            self.run_count += 1
            if self.config.StopCondition_RunCount:
                self.config.StopCondition_RunCount -= 1
            # Next battle is loading, stop conditions and task switch are checked in should_retry()
            if self.retried:
                continue
            # End
            if self.triggered_stop_condition():
                break
            # Scheduler
            if self.config.task_switched():
                self.config.task_stop()
