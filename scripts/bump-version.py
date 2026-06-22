#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import subprocess
import sys


def latest_version_tuple():
    try:
        tag = (
            subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"])
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError:
        return [0, 0, 0]
    components = [int(c) for c in tag.lstrip("v").split(".")]
    while len(components) < 3:
        components.append(0)
    return components


def main(*_):
    components = latest_version_tuple()
    components[-1] += 1
    version = ".".join(str(c) for c in components)
    subprocess.check_output(["git", "tag", version])
    print(version)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bump version and create git tag.")
    args = parser.parse_args()
    sys.exit(main(args))
