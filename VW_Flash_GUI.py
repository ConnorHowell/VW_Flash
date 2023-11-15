from nicegui import app, ui
import webview
import webview.menu as wm
import asyncio
import glob
from pathlib import Path
import os.path as path
import logging
import json
import threading
import sys
import serial
import serial.tools.list_ports

from zipfile import ZipFile
from datetime import datetime

from lib import extract_flash
from lib import binfile
from lib import flash_uds
from lib import simos_flash_utils
from lib import dsg_flash_utils
from lib import dq381_flash_utils
from lib import haldex_flash_utils
from lib import constants
from lib import simos_hsl

from lib.modules import (
    simos8,
    simos10,
    simos12,
    simos122,
    simos18,
    simos1810,
    simos184,
    dq250mqb,
    dq381,
    simos16,
    simosshared,
    haldex4motion,
)

DEFAULT_STMIN = 350000

if sys.platform == "win32":
    try:
        import winreg
    except:
        print("module winreg not found")

# Get an instance of logger, which we'll pull from the config file
logger = logging.getLogger("VWFlash")

try:
    currentPath = path.dirname(path.abspath(__file__))
except NameError:  # We are the main py2exe script, not a module
    currentPath = path.dirname(path.abspath(sys.argv[0]))

logging.config.fileConfig(path.join(currentPath, "logging.conf"))


def write_config(paths):
    with open("gui_config.json", "w") as config_file:
        json.dump(paths, config_file)


def split_interface_name(interface_string: str):
    parts = interface_string.split("_", 1)
    interface = parts[0]
    interface_name = parts[1] if len(parts) > 1 else None
    return (interface, interface_name)


def get_dlls_from_registry():
    # Interfaces is a list of tuples (name: str, interface specifier: str)
    interfaces = []
    try:
        BaseKey = winreg.OpenKeyEx(
            winreg.HKEY_LOCAL_MACHINE, r"Software\\PassThruSupport.04.04\\"
        )
    except:
        logger.warning(
            "No J2534 DLLs found in HKLM PassThruSupport. Continuing anyway."
        )
        return interfaces

    for i in range(winreg.QueryInfoKey(BaseKey)[0]):
        try:
            DeviceKey = winreg.OpenKeyEx(BaseKey, winreg.EnumKey(BaseKey, i))
            Name = winreg.QueryValueEx(DeviceKey, "Name")[0]
            FunctionLibrary = winreg.QueryValueEx(DeviceKey, "FunctionLibrary")[0]
            interfaces.append((Name, "J2534_" + FunctionLibrary))
        except:
            logger.error(
                "Found a J2534 interface, but could not enumerate the registry entry. Continuing."
            )
    return interfaces


def socketcan_ports():
    return [("SocketCAN can0", "SocketCAN_can0")]


def poll_interfaces():
    # this is a list of tuples (name: str, interface_specifier: str) where interface_specifier is something like USBISOTP_/dev/ttyUSB0
    interfaces = []

    if sys.platform == "win32":
        interfaces += get_dlls_from_registry()
    if sys.platform == "linux":
        interfaces += socketcan_ports()

    serial_ports = serial.tools.list_ports.comports()
    for port in serial_ports:
        interfaces.append(
            (port.name + " : " + port.description, "USBISOTP_" + port.device)
        )
    return interfaces


class MainUI:
    def __init__(self):
        # Window settings
        app.native.start_args["debug"] = True
        app.native.window_args["resizable"] = False
        app.native.window_args["title"] = "VW Flash GUI"

        # Init settings
        try:
            with open("gui_config.json", "r") as config_file:
                self.options = json.load(config_file)
        except:
            logger.warning("No config file present, creating one")
            self.options = {
                "tune_folder": "",
                "logger": path.join(currentPath, "logs"),
                "interface": "",
                "singlecsv": False,
                "logmode": "22",
                "activitylevel": "INFO",
            }
            write_config(self.options)

        self.interfaces = poll_interfaces()

        # Pick first interface if none already selected.
        if (len(self.options["interface"])) == 0:
            if len(self.interfaces) > 0:
                self.options["interface"] = self.interfaces[0][1]
                write_config(self.options)

        # Setup UI & Run
        self.setup_ui_elements()
        ui.run(native=True, window_size=(640, 770), fullscreen=False)

    def on_startup(self):
        if self.options["tune_folder"] != "":
            self.current_folder_path = self.options["tune_folder"]
            self.update_bin_listing()

    def setup_table(self):
        self.file_table = ui.aggrid(
            {
                "columnDefs": [
                    {
                        "headerName": "Filename",
                        "field": "file",
                        "lockPosition": "left",
                        "sortable": True,
                    },
                    {
                        "headerName": "Last Modified",
                        "field": "date",
                        "width": 100,
                        "lockPosition": "right",
                        "sortable": True,
                    },
                ],
                "rowData": [],
                "rowSelection": "single",
            }
        ).classes("min-h-40")

    def setup_ui_elements(self):
        # Main tabs
        with ui.tabs().props("dense").classes("w-full") as tabs:
            flashing = ui.tab("Flashing")
            logging = ui.tab("Logging")
            diagnostics = ui.tab("Diagnostics")

        with ui.tab_panels(tabs, value=flashing).classes("w-full"):
            with ui.tab_panel(flashing).classes("h-full p-0"):
                self.flashing_page()

            with ui.tab_panel(logging).classes("h-full p-0"):
                self.logging_page()

            with ui.tab_panel(diagnostics).classes("h-full p-0"):
                ui.label("Second tab")

    def flashing_page(self):
        # Message log to user
        self.log = (
            ui.log().classes("w-full h-50").style("resize: none; min-height: 200px;")
        )

        # Module selector & action buttons
        with ui.row().classes("w-full"):
            self.module_selection = (
                ui.select(
                    {
                        0: "Simos 18.1/6",
                        1: "Simos 18.10",
                        2: "DQ250-MQB DSG",
                        3: "DQ381 DSG UNTESTED",
                        4: "Haldex (4motion) UNTESTED",
                    },
                    value=0,
                    label="Module",
                )
                .props("outlined dense")
                .style("flex-grow: 1;")
            )

            ui.button("Get Module Info")
            ui.button("Choose Folder", on_click=self.select_folder)

        # File list table
        self.setup_table()

        # Flash progress
        self.progress = ui.linear_progress().props("stripe color=green")
        self.progress.add_slot("default", None)

        # Flash action selection & start flash button
        with ui.row().classes("w-full"):
            self.action_choice = ui.select(
                {
                    0: "Calibration Flash Unlocked",
                    1: "FlashPack ZIP flash",
                    2: "Full Flash Unlocked (BIN/FRF)",
                    3: "Flash Stock (Re-Lock) / Unmodified BIN/FRF",
                },
                value=0,
            ).props("outlined dense")

            ui.button("Flash", on_click=lambda: ui.notify("Beginning Flash!"), icon="bolt", color="green").style(
                "flex-grow: 1;"
            )

    def logging_page(self):
        # Mode selection & toggle button
        with ui.row().classes("w-full"):
            self.logging_type = (
                ui.select(
                    {
                        0: "Mode 22",
                        1: "Mode 3E (HSL)",
                    },
                    value=0,
                    label="Logging Mode",
                )
                .props("outlined dense")
                .style("flex-grow: 1;")
            )
            ui.button("Start Logging", icon="play_arrow")

    async def select_folder(self):
        folder_path = await app.native.main_window.create_file_dialog(
            webview.FOLDER_DIALOG
        )
        if folder_path and len(folder_path) > 0:
            self.current_folder_path = folder_path[0]
            self.update_bin_listing()

    def update_bin_listing(self):
        self.file_table.options["rowData"] = []
        bins = glob.glob(self.current_folder_path + "/*.bin")
        bins.extend(glob.glob(self.current_folder_path + "/*.frf"))
        self.options["tune_folder"] = self.current_folder_path

        write_config(self.options)

        bins.sort(key=path.getmtime, reverse=True)

        for bin_file in bins:
            self.file_table.options["rowData"].append(
                {
                    "file": path.basename(bin_file),
                    "date": str(
                        datetime.fromtimestamp(path.getmtime(bin_file)).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    ),
                }
            )
        self.file_table.update()


main_ui = MainUI()
app.on_startup(main_ui.on_startup)
