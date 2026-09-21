"""Run a durable event sweep: python -m src.intelligence_worker [--once]."""
import argparse
import time
from .services.agents.runtime import AgentRunner
from .services.intelligence.events import EventQueue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args()
    while True:
        count = EventQueue().drain(AgentRunner())
        print(f'Processed {count} event groups.',flush=True)
        if args.once:
            return
        time.sleep(5)


if __name__ == '__main__':
    main()
