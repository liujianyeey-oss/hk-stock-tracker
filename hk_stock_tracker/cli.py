"""Installed CLI entry point; runtime data defaults outside site-packages."""
import argparse
import os
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description='港股筹码研究工具')
    parser.add_argument('tool', choices=('track', 'backtest', 'research', 'data', 'observe', 'replay', 'sensitivity'), help='选择工具；后接该工具参数')
    if len(sys.argv) == 1 or sys.argv[1] in ('-h', '--help'):
        parser.parse_args()
        return 0
    args = parser.parse_args(sys.argv[1:2])
    rest = sys.argv[2:]
    os.environ.setdefault('HK_TRACKER_HOME', os.path.expanduser('~/.local/share/hk-stock-tracker'))
    module = {'track': 'tracker', 'data': 'research_data'}.get(args.tool, args.tool)
    sys.argv = [f'hk-tracker {args.tool}', *rest]
    qualified = f'{__package__}.{module}' if __package__ else module
    runpy.run_module(qualified, run_name='__main__')
    return 0
