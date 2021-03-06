#!/usr/bin/python3
# Copyright (c) 2020 SUSE LLC
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from binascii import hexlify


# filename -> { url: <string>, sum: <string>, path = set([<string>, ..]) }
MODULE_MAP = dict()


def collect_deps_recursive(d, deps):
    for module in sorted(deps):
        path = "/".join(("node_modules", module))
        if d:
            path = "/".join((d, path))
        entry = deps[module]
        if "bundled" in entry and entry["bundled"]:
            continue
        elif "resolved" not in entry:
            if "from" in entry:
                o = urllib.parse.urlparse(entry["from"])
                if o.scheme in ("git+http", "git+https"):
                    _, scheme = o.scheme.split("+")
                    branch = "master"
                    if o.fragment:
                        branch = o.fragment
                    p = os.path.basename(o.path)
                    if p.endswith(".git"):
                        p = p[:-4]
                    fn = "{}-{}.tgz".format(p, branch)
                    MODULE_MAP[fn] = {
                        "scm": "git",
                        "branch": branch,
                        "basename": p,
                        "url": urllib.parse.urlunparse(
                            (scheme, o.netloc, o.path, o.params, o.query, None)
                        ),
                    }

                    MODULE_MAP[fn].setdefault("path", set()).add(path)

                else:
                    logging.warning(
                        "entry %s is from unsupported location %s",
                        module,
                        entry["from"],
                    )

            else:
                logging.warning("entry %s has no download", module)
        else:
            url = entry["resolved"]
            algo, chksum = entry["integrity"].split("-", 2)
            chksum = hexlify(b64decode(chksum)).decode("ascii")
            fn = os.path.basename(url)
            # looks like some module are from some kind of branch and
            # may use the same file name. So prefix with this
            # namespace.
            if "/" in module:
                fn = module.split("/")[0] + "-" + fn
            if fn in MODULE_MAP:
                if (
                    MODULE_MAP[fn]["url"] != url
                    or MODULE_MAP[fn]["algo"] != algo
                    or MODULE_MAP[fn]["chksum"] != chksum
                ):
                    logging.error(
                        "%s: mismatch %s <> %s, %s:%s <> %s:%s",
                        module,
                        MODULE_MAP[fn]["url"],
                        url,
                        MODULE_MAP[fn]["algo"],
                        MODULE_MAP[fn]["chksum"],
                        algo,
                        chksum,
                    )
            else:
                MODULE_MAP[fn] = {"url": url, "algo": algo, "chksum": chksum}

            MODULE_MAP[fn].setdefault("path", set()).add(path)

        if "dependencies" in entry:
            collect_deps_recursive(path, entry["dependencies"])


def main(args):
    logging.info("main")

    with open(args.input) as fh:
        js = json.load(fh)

    if "dependencies" in js:
        collect_deps_recursive("", js["dependencies"])

    if args.output:
        with open(args.output, "w") as fh:
            i = 100
            for fn in sorted(MODULE_MAP):
                fh.write("Source{}:     {}#/{}\n".format(i, MODULE_MAP[fn]["url"], fn))
                i += 1

    if args.checksums:
        with open(args.checksums, "w") as fh:
            for fn in sorted(MODULE_MAP):
                fh.write(
                    "{} ({}) = {}\n".format(
                        MODULE_MAP[fn]["algo"].upper(), fn, MODULE_MAP[fn]["chksum"]
                    )
                )

    if args.locations:
        with open(args.locations, "w") as fh:
            for fn in sorted(MODULE_MAP):
                fh.write("{} {}\n".format(fn, " ".join(sorted(MODULE_MAP[fn]["path"]))))

    if args.download:
        for fn in sorted(MODULE_MAP):
            url = MODULE_MAP[fn]["url"]
            if "scm" in MODULE_MAP[fn]:
                d = MODULE_MAP[fn]["basename"]
                if os.path.exists(d):
                    r = subprocess.run(["git", "remote", "update"], cwd=d)
                    if r.returncode:
                        logging.error("failed to clone %s", url)
                        continue
                else:
                    r = subprocess.run(["git", "clone", "--bare", url, d])
                    if r.returncode:
                        logging.error("failed to clone %s", url)
                        continue
                r = subprocess.run(
                    [
                        "git",
                        "archive",
                        "--format=tar.gz",
                        "-o",
                        "../" + fn,
                        "--prefix",
                        "package/",
                        MODULE_MAP[fn]["branch"],
                    ],
                    cwd=d,
                )
                if r.returncode:
                    logging.error("failed to create tar %s", url)
                    continue
            else:
                req = urllib.request.Request(url)
                if os.path.exists(fn):
                    if args.download_skip_existing:
                        logging.info("skipping download of existing %s", fn)
                        continue
                    stamp = time.strftime(
                        "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(os.path.getmtime(fn))
                    )
                    logging.debug("adding If-Modified-Since %s: %s", fn, stamp)
                    req.add_header("If-Modified-Since", stamp)

                logging.info("fetching %s as %s", url, fn)
                algo = MODULE_MAP[fn]["algo"]
                chksum = MODULE_MAP[fn]["chksum"]
                h = hashlib.new(algo)
                response = urllib.request.urlopen(req)
                try:
                    data = response.read()
                    h.update(data)
                    if h.hexdigest() != chksum:
                        logging.error(
                            "checksum failure for %s %s %s %s",
                            fn,
                            algo,
                            h.hexdigest,
                            chksum,
                        )
                    else:
                        try:
                            with open(fn + ".new", "wb") as fh:
                                fh.write(data)
                        except OSError as e:
                            logging.error(e)
                        finally:
                            os.rename(fn + ".new", fn)
                except urllib.error.HTTPError as e:
                    logging.error(e)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="boilerplate python commmand line program"
    )
    parser.add_argument("--dry", action="store_true", help="dry run")
    parser.add_argument("--debug", action="store_true", help="debug output")
    parser.add_argument("--verbose", action="store_true", help="verbose")
    parser.add_argument(
        "-i",
        "--input",
        metavar="FILE",
        default="package-lock.json",
        help="input package lock file",
    )
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="spec files source lines into that file"
    )
    parser.add_argument(
        "--checksums", metavar="FILE", help="Write BSD style checksum file"
    )
    parser.add_argument(
        "--locations", metavar="FILE", help="Write locations into that file"
    )
    parser.add_argument("--download", action="store_true", help="download files")
    parser.add_argument(
        "--download-skip-existing",
        action="store_true",
        help="don't download existing files again",
    )

    args = parser.parse_args()

    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(format='%(levelname)s:%(message)s', level=level)

    sys.exit(main(args))

# vim: sw=4 et
