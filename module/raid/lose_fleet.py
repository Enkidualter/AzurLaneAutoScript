from module.base.button import Button, ButtonGrid
from module.combat.emotion import DIC_LIMIT
from module.logger import logger
from module.raid.assets import RAID_FLEET_PREPARATION
from module.raid.raid import raid_entrance
from module.retire.assets import DOCK_CHECK
from module.retire.dock import DOCK_SCROLL, Dock
from module.base.timer import Timer
from module.retire.scanner import LevelScanner, ShipScanner
from module.ui.page import page_raid

# Ship slots on raid fleet select page (艦隊選擇)
# Left 3 slots are main fleet (后排主力), right 3 slots are vanguard (前排先锋)
FLEET_MAIN = ButtonGrid(
    origin=(393, 155), delta=(101.5, 0), button_shape=(78, 85), grid_shape=(3, 1), name='RAID_FLEET_MAIN')
FLEET_VANGUARD = ButtonGrid(
    origin=(708, 155), delta=(101.5, 0), button_shape=(78, 85), grid_shape=(3, 1), name='RAID_FLEET_VANGUARD')
# Close button of fleet select page, click only, never matched
FLEET_SELECT_QUIT = Button(
    area=(1128, 66, 1192, 117), color=(), button=(1128, 66, 1192, 117), name='RAID_FLEET_SELECT_QUIT')
# Emotion recorded after a whole fleet is changed
CHANGED_FLEET_EMOTION = 119
# Non-collab factions, to exclude META, TEMPESTA, and others
FACTION_ALL = ['eagle', 'royal', 'sakura', 'iron', 'dragon', 'sardegna',
               'northern', 'iris', 'vichya', 'tulipa', 'pedreria', 'meta', 'tempesta', 'other']


class RaidLoseFleet(Dock):
    """
    Change ships of raid easy fleet, to keep farming affinity when all stage fleets are out of emotion.
    Ships are sorted by intimacy in ascending order, so ships with full affinity are never selected.
    """

    def get_vanguard_faction(self):
        """
        Returns:
            list[str]: Factions in dock filter
        """
        faction = [f.strip().lower() for f in self.config.RaidLoseFleet_VanguardFaction.split('>')]
        faction = [f for f in faction if f in FACTION_ALL]
        if not faction:
            logger.warning('No valid faction in RaidLoseFleet_VanguardFaction, use all factions')
            return 'all'
        return faction

    def get_min_emotion(self, stage='easy'):
        """
        Returns:
            int: Minimum emotion of a ship to run one more battle
        """
        control = getattr(self.config, f'RaidLoseEmotion_{stage.capitalize()}Control')
        return DIC_LIMIT[control] + self.emotion.reduce_per_battle

    def fleet_select_ship(self, slot, index, faction, level, min_emotion):
        """
        Open dock from a fleet slot and select a ship with the lowest intimacy.
        Ships already in this raid fleet are in status 'in_event_fleet', so they won't be selected again.

        Args:
            slot (Button): Ship slot on fleet select page
            index (str): Dock index filter, 'vanguard' or 'main'
            faction (str, list): Dock faction filter
            level (tuple): (lower, upper)
            min_emotion (int):

        Returns:
            Ship: Selected ship, or None if no ship matched

        Pages:
            in: RAID_FLEET_PREPARATION
            out: RAID_FLEET_PREPARATION
        """
        self.ui_click(slot, appear_button=RAID_FLEET_PREPARATION, check_button=DOCK_CHECK,
                      offset=(20, 20), retry_wait=3, skip_first_screenshot=True)

        self.dock_favourite_set(False, wait_loading=False)
        # Ascending, ships with the lowest intimacy first
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(index=index, faction=faction, sort='intimacy')

        scanner = ShipScanner(level=level, emotion=(min_emotion, 150), fleet=0, status='free')
        scanner.disable('rarity')
        ship = self.dock_scan_pages(scanner)

        if ship is None:
            logger.warning(f'No ship matched for slot {slot}, keep current ship')
            self.dock_reset()
            self.dock_quit()
            return None

        logger.info(f'Select ship level={ship.level} emotion={ship.emotion}')
        self.dock_select_one(ship.button)
        self.dock_reset()
        self.dock_select_confirm(check_button=RAID_FLEET_PREPARATION)
        return ship

    def wait_dock_cards_loaded(self, timeout=3):
        """
        Ship cards are rendered after scrolling, level ocr gets 0 on a blank card,
        wait until cards are rendered, otherwise ships would be considered not matched.

        Returns:
            bool: If cards loaded

        Pages:
            in: DOCK_CHECK
        """
        scanner = LevelScanner()
        timer = Timer(timeout, count=int(timeout / 0.3)).start()
        prev = None
        while 1:
            levels = scanner.scan(self.device.image, output=False)
            # A full page is rendered
            if levels and all(level > 0 for level in levels):
                return True
            # Last page may have empty cards, wait until ocr results stop changing
            if levels and any(level > 0 for level in levels) and levels == prev:
                return True
            if timer.reached():
                logger.warning(f'Wait dock cards loading timeout, levels: {levels}')
                return False

            prev = levels
            self.device.screenshot()

    def dock_scan_pages(self, scanner, max_page=10):
        """
        Scan dock page by page, dock list is sorted by intimacy in ascending order,
        so the first matched ship is the one with the lowest intimacy.

        Args:
            scanner (ShipScanner):
            max_page (int): Max pages to scan

        Returns:
            Ship: First matched ship, or None

        Pages:
            in: DOCK_CHECK
            out: DOCK_CHECK
        """
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.handle_dock_cards_loading()

        for page in range(max_page):
            self.wait_dock_cards_loaded()
            ships = scanner.scan(self.device.image, output=True)
            if ships:
                return ships[0]

            if not DOCK_SCROLL.appear(main=self) or DOCK_SCROLL.at_bottom(main=self):
                logger.info(f'No more dock pages, scanned {page + 1} pages')
                return None
            logger.info(f'No ship matched in page {page + 1}, next page')
            DOCK_SCROLL.next_page(main=self)
            # Scrolling through dock pages is not a stuck, clear click record
            self.device.click_record_clear()
            self.device.stuck_record_clear()
            self.handle_dock_cards_loading()

        logger.warning(f'Reached max dock pages {max_page}')
        return None

    def raid_fleet_change(self, raid, stage='easy'):
        """
        Change all ships of raid easy fleet, 3 vanguards and 1 main.
        Ships already in other fleets are not selected, so hard and normal fleets are untouched.

        Args:
            raid (str): Raid name
            stage (str):

        Returns:
            int: Emotion of the new fleet, 0 if not all ships are changed

        Pages:
            in: page_raid
            out: RAID_FLEET_PREPARATION
        """
        logger.hr(f'Raid fleet change, stage {stage}', level=1)
        min_emotion = self.get_min_emotion(stage)
        logger.attr('Min emotion', min_emotion)

        # Enter fleet select page
        self.ui_click(raid_entrance(raid=raid, mode=stage), appear_button=page_raid.check_button,
                      check_button=RAID_FLEET_PREPARATION, offset=(20, 20), retry_wait=3,
                      skip_first_screenshot=True)

        emotions = []
        # Vanguard, 3 ships with faction and level limit
        for slot in FLEET_VANGUARD.buttons:
            ship = self.fleet_select_ship(
                slot=slot, index='vanguard', faction=self.get_vanguard_faction(),
                level=(self.config.RaidLoseFleet_VanguardLevelMin, self.config.RaidLoseFleet_VanguardLevelMax),
                min_emotion=min_emotion)
            if ship is None:
                return 0
            emotions.append(ship.emotion)
        # Main, 1 ship, no faction limit
        ship = self.fleet_select_ship(
            slot=FLEET_MAIN.buttons[0], index='main', faction='all',
            level=(self.config.RaidLoseFleet_MainLevelMin, self.config.RaidLoseFleet_MainLevelMax),
            min_emotion=min_emotion)
        if ship is None:
            return 0
        emotions.append(ship.emotion)

        # Dock emotion ocr reads the mood icon which is not accurate,
        # all ships are changed so just record a full emotion
        logger.info(f'Raid fleet changed, scanned emotion: {emotions}, '
                    f'record fleet emotion as {CHANGED_FLEET_EMOTION}')
        return CHANGED_FLEET_EMOTION

    def raid_fleet_select_quit(self):
        """
        Pages:
            in: RAID_FLEET_PREPARATION
            out: page_raid
        """
        logger.info('Raid fleet select quit')
        self.ui_click(FLEET_SELECT_QUIT, appear_button=RAID_FLEET_PREPARATION,
                      check_button=page_raid.check_button, offset=(20, 20), retry_wait=3,
                      skip_first_screenshot=True)
