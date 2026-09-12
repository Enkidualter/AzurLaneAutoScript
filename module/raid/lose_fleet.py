from dataclasses import replace

import cv2
import numpy as np

from module.base.button import Button, ButtonGrid
from module.base.timer import Timer
from module.base.utils import crop
from module.combat.emotion import DIC_LIMIT
from module.logger import logger
from module.raid.assets import RAID_FLEET_PREPARATION
from module.raid.raid import raid_entrance
from module.retire.assets import DOCK_CHECK
from module.retire.dock import DOCK_SCROLL, Dock
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
# Ship card layout in dock, see CARD_GRIDS in module/retire/dock.py
CARD_AREA = (93, 0, 1218, 720)
CARD_ROW_TOP = [76, 303]
CARD_ROW_DELTA = 227
CARD_GAP_MIN = 8
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

    @staticmethod
    def dock_card_offset(image):
        """
        Dock scrolls by pixel, so ship cards are not aligned to CARD_GRIDS after scrolling,
        an offset of 10px is enough to make level ocr read nothing.
        Gaps between card rows are flat background having a much lower std,
        so card rows can be located by finding the gaps.

        Returns:
            int: Vertical offset of ship cards against CARD_GRIDS, or None if no gap found
        """
        strip = crop(image, CARD_AREA, copy=False)
        gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        std = np.std(gray, axis=1)
        flat = std < np.mean(std) * 0.5

        # Bottom of a gap is the top of a card row
        tops = []
        count = 0
        for y, is_flat in enumerate(flat):
            if is_flat:
                count += 1
            else:
                if count >= CARD_GAP_MIN:
                    tops.append(y)
                count = 0
        if not tops:
            return None

        offsets = [min([top - expected for expected in CARD_ROW_TOP], key=abs) for top in tops]
        offsets = [offset for offset in offsets if abs(offset) <= CARD_ROW_DELTA // 2]
        if not offsets:
            return None
        return int(np.median(offsets))

    def dock_scan_aligned(self, scanner, timeout=3):
        """
        Scan ships on current dock page, aligning cards to CARD_GRIDS first.
        Cards are also rendered one by one after scrolling, so retry until any level is read.

        Args:
            scanner (ShipScanner):
            timeout (int, float):

        Returns:
            list[Ship]: Matched ships, buttons are moved to where cards really are

        Pages:
            in: DOCK_CHECK
        """
        timer = Timer(timeout, count=int(timeout / 0.3)).start()
        while 1:
            image = self.device.image
            offset = self.dock_card_offset(image)
            if offset is not None:
                aligned = np.roll(image, -offset, axis=0)
                levels = LevelScanner().scan(aligned, output=False)
                if levels and any(level > 0 for level in levels):
                    logger.attr('Dock card offset', offset)
                    ships = scanner.scan(aligned, output=True)
                    # Ship is a frozen dataclass, rebuild them with buttons at real positions
                    return [replace(ship, button=ship.button.move((0, offset))) for ship in ships]
            if timer.reached():
                logger.warning('Scan dock page timeout, no ship card was read')
                return []

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
            ships = self.dock_scan_aligned(scanner)
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
