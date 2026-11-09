"""Build the Windows online installer and release checksums."""
from installer.build import build, ROOT, VERSION


if __name__ == '__main__':
    build(ROOT.parent / 'releases' / ('v' + VERSION))
