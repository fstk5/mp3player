from __future__ import annotations

import os
import random
import time
import traceback
from gc import collect
from traceback import print_exception

import adafruit_displayio_ssd1306
import adafruit_tlv320
import alarm
import analogio
import audiobusio
import audiocore
import audiomp3
import board
import busio
import displayio
import i2cdisplaybus
import sdcardio
import storage
import terminalio
from adafruit_display_text import label
from adafruit_pathlib import Path
from adafruit_seesaw.seesaw import Seesaw
from alarm import pin

import metadata

# Configuration constants
BUTTON_HOLD_THRESHOLD_POWER = 2500000000  # 2.5 seconds in nanoseconds
BUTTON_HOLD_THRESHOLD_DEFAULT = 1000000000  # 1 second in nanoseconds
VOLTAGE_DIVIDER_RATIO = 2.0
ADC_MAX_VALUE = 65535
ADC_REF_VOLTAGE = 3.3
AUDIO_STOP_DELAY = 0.05  # seconds to wait after stopping audio


def boot():
    spi = board.SPI()
    sdcard = sdcardio.SDCard(spi, board.D7, 16000000)
    # We can ignore this warning since the Adafruit `SDCard` class implements the functions required.
    # noinspection PyTypeChecker
    vfs = storage.VfsFat(sdcard)  # ty:ignore[invalid-argument-type]
    try:
        os.mkdir("/sd")
    except:
        pass
    storage.mount(vfs, "/sd", readonly=False)
    collect()
    return vfs


def shuffle(lst):
    for i in range(len(lst) - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]
    return lst


class InputHandler:
    def __init__(
            self,
            targetPin: int,
            audio_manager: AudioManager,
            seesaw: Seesaw,
            display_manager: DisplayManager,
    ) -> None:
        self.audio_manager = audio_manager
        self.seesaw = seesaw
        self.pin = targetPin
        self.should_power_off = False
        self.button_was_pressed = False
        self.press_start_time = 0
        self.hold_triggered = False
        if self.pin == 1:
            self.hold_threshold = BUTTON_HOLD_THRESHOLD_POWER
        else:
            self.hold_threshold = BUTTON_HOLD_THRESHOLD_DEFAULT
        self.display_manager = display_manager

    def check_button(self) -> None:
        if self.seesaw.digital_read(self.pin) == 0 and not self.button_was_pressed:
            # Button was just pressed down, do not trigger any event yet
            self.press_start_time = time.time_ns()
            self.button_was_pressed = True
            self.hold_triggered = False

        elif self.seesaw.digital_read(self.pin) == 0 and self.button_was_pressed:
            # Button is currently being held down
            if (
                    not self.hold_triggered
                    and (time.time_ns() - self.press_start_time) >= self.hold_threshold
            ):
                self.hold_action()

        elif self.seesaw.digital_read(self.pin) != 0 and self.button_was_pressed:
            # Button was just released
            if not self.hold_triggered:
                self.click_action()

            self.button_was_pressed = False
            self.hold_triggered = False

    def hold_action(self) -> None:
        self.hold_triggered = True
        if self.pin == 1:
            self.set_should_power_off()
        elif self.pin == 3:
            self.audio_manager.toggle_song_repeat()

    def click_action(self) -> None:
        if self.pin == 1:
            self.audio_manager.toggle_audio()
        elif self.pin == 2:
            self.audio_manager.increase_volume()
        elif self.pin == 3:
            self.audio_manager.change_audio("backward")
        elif self.pin == 4:
            self.audio_manager.decrease_volume()
        elif self.pin == 5:
            self.audio_manager.change_audio("forward")

    def set_should_power_off(self) -> None:
        self.should_power_off = True


class AudioManager:
    def __init__(self, i2c, seesaw: Seesaw) -> None:
        # Queue related setup
        self.queue: list[str] = []
        self.current_progress = -1
        self.current_audio_data: tuple[str, str] = "Title", "Artist"
        self.should_loop = False
        self.is_paused = False

        # Audio decoders
        self.mp3_file = Path("/assets/empty.mp3").open("rb")
        self.wav_file = Path("/assets/empty.wav").open("rb")
        self.current_decoder = None
        self.reset_audio_list()

        self.audio_out = audiobusio.I2SOut(
            board.D3,
            board.D6,
            board.D2
        )

        # Volume related setup
        self.volume = -25
        self.dac = self.configure_dac(seesaw, i2c)

        self.current_sample_rate = None

        self._elapsed_time = 0
        self._start_time = 0
        self.is_currently_playing = False

    def reset_audio_list(self) -> None:
        clean_list = []
        try:
            for file in Path("/sd").iterdir():
                if file.lower().endswith(".wav") or file.lower().endswith(".mp3"):
                    clean_list.append(file)
            self.queue = clean_list
            self.shuffle_audio_list()
        except OSError:
            print("Error accessing SD card.")
        del clean_list
        collect()

    def shuffle_audio_list(self) -> None:
        shuffle(self.queue)

    def toggle_audio(self) -> None:
        if self.is_paused:
            self.is_paused = False
            if self.audio_out.paused:
                self.audio_out.resume()
            # Reset start time to account for pause duration
            self._start_time = time.time() - self._elapsed_time
        else:
            self.is_paused = True
            if self.audio_out.playing and not self.audio_out.paused:
                self.audio_out.pause()

    def update_sample_rate(self, sample_rate: int) -> None:
        if self.current_sample_rate == sample_rate:
            return
        self.dac.configure_clocks(sample_rate)
        self.current_sample_rate = sample_rate

    def get_next_audio(self) -> str:
        if not self.queue:
            return ""
        self.current_progress += 1
        if self.current_progress >= len(self.queue):
            self.current_progress = 0
        return self.queue[self.current_progress]

    def get_previous_audio(self) -> str:
        if not self.queue:
            return ""
        self.current_progress -= 1
        if self.current_progress < 0:
            self.current_progress = len(self.queue) - 1
        return self.queue[self.current_progress]

    def get_repeat_audio(self) -> str:
        if (
                not self.queue
                or self.current_progress < 0
                or self.current_progress >= len(self.queue)
        ):
            return ""
        else:
            return self.queue[self.current_progress]

    def change_audio(self, skipDirection: str, retry_count: int = 0) -> None:
        audio_file_path = ""

        if skipDirection == "forward":
            audio_file_path = "/sd/" + self.get_next_audio()
        elif skipDirection == "backward":
            audio_file_path = "/sd/" + self.get_previous_audio()
        elif skipDirection == "restart":
            audio_file_path = "/sd/" + self.get_repeat_audio()

        if not self.queue or audio_file_path == "/sd/":
            audio_file_path = "/assets/empty.wav"

        try:
            # Stop current playback if active
            if self.audio_out.playing:
                self.audio_out.stop()
            time.sleep(AUDIO_STOP_DELAY)

            try:
                data = metadata.get_song_metadata(audio_file_path)
            except Exception as e:
                print("Error accessing metadata.")
                print("Could not parse ID3 metadata:", e)
                data = None
                traceback.print_exception(e)

            # Extract title and artist safely
            if data is not None and data["title"] is not None:
                title = data["title"]
            else:
                title: str = audio_file_path.removeprefix("/sd/")

            if data is not None and data["artist"] is not None:
                artist = data["artist"]
            else:
                artist = "Unknown Artist"

            self.current_audio_data = title, artist

            del data, title, artist
            collect()

            if audio_file_path.lower().endswith(".wav"):
                if self.wav_file:
                    self.wav_file.close()
                self.wav_file = Path(audio_file_path).open("rb")
                wav_decoder = audiocore.WaveFile(self.wav_file, bytearray(4096))
                self.current_decoder = wav_decoder

            elif audio_file_path.lower().endswith(".mp3"):
                if self.mp3_file:
                    self.mp3_file.close()
                self.mp3_file = Path(audio_file_path).open( "rb")
                mp3_decoder = audiomp3.MP3Decoder(self.mp3_file, bytearray(4096))
                self.current_decoder = mp3_decoder

            if self.current_decoder is not None:
                # Play directly through I2SOut
                self.audio_out.play(self.current_decoder)

                # Update DAC for this sample rate while BCLK is actively running
                self.update_sample_rate(self.current_decoder.sample_rate)
            else:
                if retry_count < len(self.queue):
                    self.change_audio("forward", retry_count + 1)
                else:
                    self.current_audio_data = "No Audio", "Check SD Card"

            self._start_time = time.time()
            self.is_currently_playing = True
            self.is_paused = False

        except Exception as e:
            print(f"Error opening {audio_file_path}. Error: {e}")
            if retry_count < len(self.queue):
                self.change_audio("forward", retry_count + 1)
            else:
                print("All files failed to load, falling back to empty audio.")
                self.current_audio_data = "No Audio", "Check SD Card"

        del audio_file_path
        collect()

    def get_elapsed_time(self) -> int:
        if self.is_currently_playing and not self.is_paused:
            self._elapsed_time = round(time.time() - self._start_time)
        return self._elapsed_time

    def toggle_song_repeat(self) -> None:
        self.should_loop = not self.should_loop

    def song_end_listener(self) -> None:
        # Check if audio stopped playing (song ended)
        if self.is_currently_playing and not self.is_paused and not self.audio_out.playing:
            self.is_currently_playing = False
            self.change_audio("restart" if self.should_loop else "forward")

    def configure_dac(self, seesaw: Seesaw, i2c) -> adafruit_tlv320.TLV320DAC3100:
        reset_pin = 9
        seesaw.pin_mode(reset_pin, seesaw.OUTPUT)
        seesaw.digital_write(reset_pin, False)
        time.sleep(0.1)
        seesaw.digital_write(reset_pin, True)

        del reset_pin, seesaw
        collect()

        dac = adafruit_tlv320.TLV320DAC3100(i2c)

        # Configure DAC with initial sample rate (will be updated per file)
        # Start with 44100 Hz as default
        dac.configure_clocks(44100, 16)

        dac.headphone_output = True
        dac.dac_volume = -10
        dac.headphone_volume = self.volume
        dac.speaker_volume = self.volume

        dac.speaker_output = False

        return dac

    def increase_volume(self) -> None:
        self.volume = max(-75, min(self.volume + 1, 0))
        self.dac.headphone_volume = self.volume

    def decrease_volume(self) -> None:
        self.volume = max(-75, min(self.volume - 1, 0))
        self.dac.headphone_volume = self.volume

    def stop_audio(self) -> None:
        if self.audio_out.playing:
            self.audio_out.stop()
        self.is_currently_playing = False
        self.is_paused = False
        self.current_progress = -1
        self.current_audio_data = "Title", "Artist"
        if self.mp3_file:
            self.mp3_file.close()
        if self.wav_file:
            self.wav_file.close()
        collect()


class DisplayManager:
    def __init__(self, i2c: busio.I2C) -> None:
        display_bus = i2cdisplaybus.I2CDisplayBus(i2c, device_address=0x3C)
        self.display_device = adafruit_displayio_ssd1306.SSD1306(
            display_bus, width=128, height=32
        )
        self.display = self.display_device
        self.display.auto_refresh = False
        self.display.rotation = 180
        self.splash = displayio.Group()
        self.splash.append(displayio.Group())
        self.display.root_group = self.splash

        self.info_group = displayio.Group()
        self.title_text = label.Label(
            terminalio.FONT, text="", color=0xFFFFFF, x=16, y=0
        )
        self.artist_text = label.Label(
            terminalio.FONT, text="", color=0xFFFFFF, x=16, y=8
        )
        self.elapsed_text = label.Label(
            terminalio.FONT, text="", color=0xFFFFFF, x=16, y=24
        )

        # initialize icons
        title_icon = displayio.OnDiskBitmap("/assets/title.bmp")
        self.drawn_title_icon = displayio.TileGrid(
            title_icon, pixel_shader=displayio.ColorConverter(), x=0, y=0
        )

        artist_icon = displayio.OnDiskBitmap("/assets/artist.bmp")
        self.drawn_artist_icon = displayio.TileGrid(
            artist_icon, pixel_shader=displayio.ColorConverter(), x=0, y=8
        )

        paused_icon = displayio.OnDiskBitmap("/assets/paused.bmp")
        self.drawn_paused_icon = displayio.TileGrid(
            paused_icon, pixel_shader=displayio.ColorConverter(), x=0, y=25
        )

        playing_icon = displayio.OnDiskBitmap("/assets/playing.bmp")
        self.drawn_playing_icon = displayio.TileGrid(
            playing_icon, pixel_shader=displayio.ColorConverter(), x=0, y=25
        )

        enabled_repeat_icon = displayio.OnDiskBitmap("/assets/repeaton.bmp")
        self.drawn_enabled_repeat_icon = displayio.TileGrid(
            enabled_repeat_icon, pixel_shader=displayio.ColorConverter(), x=56, y=8
        )

        del (
            title_icon,
            artist_icon,
            paused_icon,
            playing_icon,
            enabled_repeat_icon,
        )
        collect()

        battery_level_0_icon = displayio.OnDiskBitmap("/assets/batt0.bmp")
        battery_level_1_icon = displayio.OnDiskBitmap("/assets/batt1.bmp")
        battery_level_2_icon = displayio.OnDiskBitmap("/assets/batt2.bmp")
        battery_level_3_icon = displayio.OnDiskBitmap("/assets/batt3.bmp")
        battery_level_charging_icon = displayio.OnDiskBitmap("/assets/battcharge.bmp")
        self.drawn_battery_icons = [
            displayio.TileGrid(
                battery_level_0_icon,
                pixel_shader=displayio.ColorConverter(),
                x=112,
                y=0,
            ),
            displayio.TileGrid(
                battery_level_1_icon,
                pixel_shader=displayio.ColorConverter(),
                x=112,
                y=0,
            ),
            displayio.TileGrid(
                battery_level_2_icon,
                pixel_shader=displayio.ColorConverter(),
                x=112,
                y=0,
            ),
            displayio.TileGrid(
                battery_level_3_icon,
                pixel_shader=displayio.ColorConverter(),
                x=112,
                y=0,
            ),
            displayio.TileGrid(
                battery_level_charging_icon,
                pixel_shader=displayio.ColorConverter(),
                x=112,
                y=0,
            ),
        ]

        del (
            battery_level_0_icon,
            battery_level_1_icon,
            battery_level_2_icon,
            battery_level_3_icon,
            battery_level_charging_icon,
        )
        collect()

        self.info_group.append(self.drawn_title_icon)
        self.info_group.append(self.title_text)
        self.info_group.append(self.drawn_artist_icon)
        self.info_group.append(self.artist_text)
        self.info_group.append(self.elapsed_text)

    def update_base_display(
            self,
            data: tuple[str, str],
            elapsed: int,
            is_paused: bool,
            is_repeating: bool,
            voltage: float,
    ) -> None:
        # Safely clear the info_group
        while len(self.info_group) > 0:
            self.info_group.pop()

        # Update label text strings (do not create new Label objects!)
        self.title_text.text = data[0]
        self.artist_text.text = data[1]
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_text.text = f"{minutes}:{seconds:02d}"

        # Append static base layers back
        self.info_group.append(self.drawn_title_icon)
        self.info_group.append(self.title_text)
        self.info_group.append(self.drawn_artist_icon)
        self.info_group.append(self.artist_text)
        self.info_group.append(self.elapsed_text)

        # Append dynamic status icons
        if is_paused:
            self.info_group.append(self.drawn_paused_icon)
        else:
            self.info_group.append(self.drawn_playing_icon)

        if is_repeating:
            self.info_group.append(self.drawn_enabled_repeat_icon)

        if voltage > 4.2:
            self.info_group.append(self.drawn_battery_icons[4])
        elif voltage < 3.6:
            self.info_group.append(self.drawn_battery_icons[0])
        elif voltage < 3.7:
            self.info_group.append(self.drawn_battery_icons[1])
        elif voltage < 3.8:
            self.info_group.append(self.drawn_battery_icons[2])
        else:
            self.info_group.append(self.drawn_battery_icons[3])

        self.display.refresh()
        collect()

    def shutdown_display(self) -> None:
        self.splash.pop()
        shutdown_group = displayio.Group()
        shutdown_text = label.Label(
            terminalio.FONT, text="Shutting down...", color=0xFFFFFF, x=16, y=8
        )
        shutdown_group.append(shutdown_text)
        self.splash.append(shutdown_group)
        self.display.refresh()
        time.sleep(1)
        self.display.sleep()
        del (
            shutdown_group,
            shutdown_text,
            self.splash,
            self.display,
            self.display_device,
            self.drawn_title_icon,
            self.drawn_artist_icon,
            self.drawn_paused_icon,
            self.drawn_playing_icon,
            self.drawn_enabled_repeat_icon,
            self.drawn_battery_icons,
        )
        collect()


class MP3Player:
    def __init__(self) -> None:
        self.vfs = boot()
        displayio.release_displays()
        self.i2c = board.I2C()
        self.seesaw = Seesaw(self.i2c)
        pin_mask = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
        self.seesaw.pin_mode_bulk(pin_mask, self.seesaw.INPUT_PULLUP)
        self.audio_manager = AudioManager(self.i2c, self.seesaw)
        self.display_manager = DisplayManager(self.i2c)
        self.input_handlers = [
            InputHandler(
                0, self.audio_manager, self.seesaw, self.display_manager
            ),  # select
            InputHandler(
                1, self.audio_manager, self.seesaw, self.display_manager
            ),  # up
            InputHandler(
                2, self.audio_manager, self.seesaw, self.display_manager
            ),  # left
            InputHandler(
                3, self.audio_manager, self.seesaw, self.display_manager
            ),  # down
            InputHandler(
                20, self.audio_manager, self.seesaw, self.display_manager
            ),  # right
        ]
        self.current_system_voltage = 4.2
        self.battery_pin = analogio.AnalogIn(board.D1)

    def error_handler(self, error: Exception) -> None:
        print(error)
        try:
            with Path("/sd/error.log").open("a") as f:
                f.write(str(time.time()) + "\n")
                print_exception(error, file=f)
        except Exception as e:
            print("SD card unavailable. Error: " + str(e))
        self.power_off()

    def power_off(self) -> None:
        try:
            self.audio_manager.stop_audio()
        except Exception as e:
            print("Error stopping audio. Error: " + str(e))
        try:
            self.display_manager.shutdown_display()
        except Exception as e:
            print("Error shutting down display. Error: " + str(e))
        displayio.release_displays()
        try:
            storage.umount(self.vfs)
        except Exception as e:
            print("Error unmounting storage. Error: " + str(e))
        wakeup = pin.PinAlarm(pin=board.D0, value=False, pull=True)
        alarm.exit_and_deep_sleep_until_alarms(wakeup)

    def update_battery_voltage(self) -> None:
        raw = self.battery_pin.value
        system_voltage = (raw * ADC_REF_VOLTAGE) / ADC_MAX_VALUE
        self.current_system_voltage = round((system_voltage * VOLTAGE_DIVIDER_RATIO), 4)

    def should_power_off(self) -> bool:
        for handler in self.input_handlers:
            if handler.should_power_off:
                return True
        return 3.45 > self.current_system_voltage > 1

    def run(self) -> None:
        ticker = 0
        try:
            last_update = time.monotonic()
            while True:
                for handler in self.input_handlers:
                    handler.check_button()
                now = time.monotonic()
                self.audio_manager.song_end_listener()
                if now - last_update > 0.85:
                    self.update_battery_voltage()
                    self.display_manager.update_base_display(
                        self.audio_manager.current_audio_data,
                        self.audio_manager.get_elapsed_time(),
                        self.audio_manager.is_paused,
                        self.audio_manager.should_loop,
                        self.current_system_voltage,
                    )
                    last_update = now

                if self.should_power_off():
                    self.power_off()

                ticker += 1
                if ticker >= 480:
                    collect()
                    ticker = 0
                time.sleep(0.175)
        except Exception as e:
            self.error_handler(e)
