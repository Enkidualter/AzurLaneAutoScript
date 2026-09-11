from module.combat.assets import BATTLE_STATUS_A, BATTLE_STATUS_B, BATTLE_STATUS_S, EXP_INFO_D, OPTS_INFO_D
from module.logger import logger
from module.raid.run import RaidRun


class RaidLose(RaidRun):
    """
    Lose raid on purpose to farm affinity, raid defeat costs no emotion.
    User should set up a fleet that is sure to be defeated.
    """
    won = False

    def handle_battle_status(self, drop=None):
        if not self.won and not self.is_combat_executing():
            for button in [BATTLE_STATUS_S, BATTLE_STATUS_A, BATTLE_STATUS_B]:
                if self.appear(button):
                    logger.warning(f'Raid won unexpectedly: {button}')
                    self.won = True
                    break
        return super().handle_battle_status(drop=drop)

    def handle_exp_info(self):
        if super().handle_exp_info():
            return True
        if self.is_combat_executing():
            return False
        if self.appear_then_click(EXP_INFO_D):
            self.device.sleep((0.25, 0.5))
            return True

        return False

    def raid_expected_end(self):
        # Defeat tips page after D rank
        if self.appear_then_click(OPTS_INFO_D, offset=(30, 30), interval=3):
            return False
        return super().raid_expected_end()

    def raid_execute_once(self, mode, raid):
        """
        Pages:
            in: page_raid
            out: page_raid
        """
        # Avoid ticket popup when free attempts run out
        if self.get_remain(mode) <= 0:
            logger.info(f'No remain in raid {mode}, delay to server update')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        self.won = False
        super().raid_execute_once(mode=mode, raid=raid)

        if self.won:
            logger.critical('Fleet is supposed to be defeated in RaidLose, '
                            'task disabled to protect emotion, please check your fleet')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

    def run(self, name='', mode='', total=0):
        # Defeat costs no emotion
        self.config.override(Emotion_Mode='ignore')
        mode = mode if mode else self.config.RaidLose_Mode
        super().run(name=name, mode=mode, total=total)
