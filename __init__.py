import os
import sys


_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from kicad_footprint_builder_plugin import KiCadFootprintBuilderPlugin


KiCadFootprintBuilderPlugin().register()
