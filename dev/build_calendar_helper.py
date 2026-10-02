#!/usr/bin/python3
"""Build the optional EventKit helper; Python itself remains stdlib-only."""
import argparse
import os
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build/Apple Calendar Sync.app")
    args = parser.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    contents = os.path.join(os.path.abspath(args.output), "Contents")
    executable = os.path.join(contents, "MacOS", "CalendarExport")
    os.makedirs(os.path.dirname(executable), exist_ok=True)
    shutil.copyfile(os.path.join(root, "native", "Info.plist"), os.path.join(contents, "Info.plist"))
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-O",
                    "-module-cache-path", os.path.join(root, "build", "swift-cache"),
                    os.path.join(root, "native", "CalendarExport.swift"),
                    "-o", executable], check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", os.path.dirname(contents)], check=True)


if __name__ == "__main__":
    main()
