"""Start the Overdrive Hub engine.

    python run.py                    real cars over Bluetooth
    python run.py --sim 4            add 4 simulated cars (stored separately in data-sim/)
    python run.py --sim 4 --no-ble   simulator only, never touches real cars
"""
import argparse
import logging
from pathlib import Path

from odhub.engine import Engine
from odhub.library import Library
from odhub.server import run

ROOT = Path(__file__).parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sim", type=int, default=0, help="number of simulated cars")
    ap.add_argument("--no-ble", action="store_true", help="do not use Bluetooth")
    ap.add_argument("--data", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    data = Path(args.data) if args.data else ROOT / ("data-sim" if args.sim else "data")
    engine = Engine(Library(data), sim_cars=args.sim, use_ble=not args.no_ble)
    print(f"ODHUB_READY port={args.port} data={data}", flush=True)
    run(engine, port=args.port)


if __name__ == "__main__":
    main()
