"""Run lightweight unit tests without requiring pytest or Telegram credentials."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    count = 0
    for filename in sorted((root / 'tests').glob('test_*.py')):
        spec = spec_from_file_location(filename.stem, filename)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in sorted(dir(module)):
            if name.startswith('test_') and callable(getattr(module, name)):
                getattr(module, name)()
                print('OK', filename.name, name)
                count += 1
    print(f'{count} tests réussis')


if __name__ == '__main__':
    main()
