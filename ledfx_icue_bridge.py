#!/usr/bin/env python3
import argparse
import sys

import ledfx_icue_core as core


def build_arg_parser():
    parser = argparse.ArgumentParser(description="LedFx UDP -> Corsair iCUE bridge")
    parser.add_argument(
        "--config", default="config.json", help="Chemin vers config.json"
    )
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list-groups", action="store_true")
    parser.add_argument("--test", action="store_true", help="Test LEDs (rouge)")
    parser.add_argument(
        "--test-color",
        help="Test LEDs R,G,B (ex: 255,0,0)",
    )
    parser.add_argument("--debug-udp", action="store_true")
    parser.add_argument("--debug-icue", action="store_true")
    parser.add_argument("--mode", choices=["unique", "group", "fusion"])
    parser.add_argument("--group-port", type=int)
    parser.add_argument("--fusion-port", type=int)
    parser.add_argument("--fan-sweep", action="store_true")
    parser.add_argument("--fan-index", type=int, default=1)
    parser.add_argument("--fan-speed", type=float, default=0.08)
    parser.add_argument("--fan-group", default="ventilos")
    parser.add_argument("--fan-on", help="Allumer uniquement certains ventilos (ex: 1,2)")
    parser.add_argument("--fan-color", default="255,255,255")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    return core.run_bridge(args)


if __name__ == "__main__":
    sys.exit(main())
