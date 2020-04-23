#!/usr/bin/env python

import glob
import os
import subprocess
import sys

from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext as _build_ext
from setuptools.command.build_py import build_py as _build_py
from distutils import log as setup_log

SRCDIR = os.path.abspath(os.path.dirname(__file__))
USE_BUNDLED_LIBMECAB = "BUNDLE_LIBMECAB" in os.environ


# Read a file within the source tree that may or may not exist, and
# decode its contents as UTF-8, regardless of external locale settings.
def read_file(filename):
    filepath = os.path.join(SRCDIR, filename)
    try:
        raw = open(filepath, "rb").read()
    except (IOError, OSError):
        return ""
    return raw.decode("utf-8")


# Discard unwanted top matter from README.md for reuse as the long
# description.  Specifically, we discard everything up to and
# including the first Markdown header line (begins with a '#') and
# any blank lines immediately after that header.
def read_and_trim_readme():
    readme = read_file("README.md").splitlines()
    found_first_header = False
    start = None
    for i, line in enumerate(readme):
        # Both leading and trailing horizontal whitespace may be
        # significant in Markdown, so we don't strip any.
        if found_first_header:
            if line:
                # This is the first non-blank line after the
                # first header, and therefore the first line
                # we want to preserve.
                start = i
                break
        elif line and line[0] == '#':
            found_first_header = True
    else:
        sys.stderr.write("Failed to parse README.md\n")
        sys.exit(1)

    return "\n".join(readme[start:])


# We can build using either a local bundled copy of libmecab, or a
# system-provided one.  Delay deciding which of these to do until
# `build_ext` is invoked, because if `build_ext` isn't going to be
# invoked, we shouldn't either attempt to build the bundled copy
# or run the external `mecab-config` utility.
def maybe_build_libmecab_and_adjust_flags(ext):
    if USE_BUNDLED_LIBMECAB:
        subprocess.check_call([
            sys.executable,
            os.path.join(SRCDIR, "scripts/build-bundled-libmecab.py"),
            SRCDIR
        ])
        inc_dir  = [os.path.join(SRCDIR, "build/libmecab/mecab/src")]
        lib_dirs = [os.path.join(SRCDIR, "build/libmecab/mecab/src")]

        # mecab-config --libs-only-l will produce the list of
        # libraries needed to link with a hypothetical *shared*
        # libmecab; we built a *static* libmecab, so what we actually
        # need is -lmecab + the value of the LIBS substitution
        # variable from the Makefile.
        libs = ["mecab"]
        with open(os.path.join(SRCDIR, "build/libmecab/mecab/Makefile"),
                  "rt") as fp:
            for line in fp:
                if line.startswith("LIBS ="):
                    for lib in line.partition("=")[2].split():
                        if lib[:2] == "-l":
                            libs.append(lib[2:])
                    break

    else:
        # Ensure use of the "C" locale when invoking mecab-config.
        # ("C.UTF-8" would be better if available, but there's no
        # good way to find out whether it's available.)
        clocale_env = {}
        for k, v in os.environ.items():
            if not (k.startswith("LC_") or k == "LANG" or k == "LANGUAGE"):
                clocale_env[k] = v
        clocale_env["LC_ALL"] = "C"

        def mecab_config(arg):
            output = subprocess.check_output(["mecab-config", arg],
                                             env=clocale_env)
            if not isinstance(output, str):
                output = output.decode("utf-8")
            return output.split()

        inc_dir  = mecab_config("--inc-dir")
        lib_dirs = mecab_config("--libs-only-L")
        libs     = mecab_config("--libs-only-l")

    swig_opts = ["-O", "-builtin", "-c++"]

    if sys.version_info.major >= 3:
        swig_opts.append("-py3")

    swig_opts.extend("-I"+d for d in inc_dir)

    ext.include_dirs = inc_dir
    ext.library_dirs = lib_dirs
    ext.libraries    = libs
    ext.swig_opts    = swig_opts
    ext.extra_compile_args = ["-Wno-unused-variable"]

    sys.stderr.write("Extension build configuration adjusted:\n"
                     " include_dirs = {!r}\n"
                     " library_dirs = {!r}\n"
                     " libraries    = {!r}\n"
                     " swig_opts    = {!r}\n"
                     .format(inc_dir, lib_dirs, libs, swig_opts))


# After running SWIG, discard the unwanted Python-level wrapper
# (there doesn't seem to be any way to get SWIG not to generate this)
def discard_swig_wrappers(ext):
    SWIG_WRAPPER_MARKER = "# This file was automatically generated by SWIG"
    for src in ext.sources:
        (base, ext) = os.path.splitext(src)
        if ext == ".i":
            swig_py_wrapper = base + ".py"
            try:
                with open(swig_py_wrapper, "rt") as fp:
                    first = next(fp)
                    if not first.startswith(SWIG_WRAPPER_MARKER):
                        swig_py_wrapper = None
            except (OSError, IOError):
                swig_py_wrapper = None
            if swig_py_wrapper is not None:
                setup_log.info("discarding wrapper module {} for {}"
                               .format(swig_py_wrapper, src))
                os.unlink(swig_py_wrapper)


class build_ext(_build_ext):
    def build_extension(self, ext):
        if ext.name == "MeCab._MeCab":
            maybe_build_libmecab_and_adjust_flags(ext)
        _build_ext.build_extension(self, ext)
        if ext.name == "MeCab._MeCab":
            discard_swig_wrappers(ext)


# The bundled libmecab needs a bundled dictionary, which we copy
# from somewhere in the file system.
def dicdir_from_mecabrc(rc_fname):
    try:
        with open(rc_fname, "rt") as fp:
            for line in fp:
                line = line.strip()
                if not line or line[0] == ';':
                    continue
                if line[:6] == "dicdir":
                    line = line[6:].lstrip()
                    if not line:
                        return None

                    if line[0] == '=':
                        line = line[1:].lstrip()
                    if line:
                        return line
        return None
    except (IOError, OSError):
        return None


def mecab_dictionary_contents():
    dicdir = None
    if "MECAB_DICDIR" in os.environ:
        d = os.environ["MECAB_DICDIR"]
        if d and os.path.isdir(d):
            dicdir = os.path.abspath(d)
    if dicdir is None and "MECAB_DICPATH" in os.environ:
        for d in os.environ["MECAB_DICPATH"].split(os.pathsep):
            if d and os.path.isdir(d):
                dicdir = os.path.abspath(d)
                break
    if dicdir is None and "MECABRC" in os.environ:
        d = dicdir_from_mecabrc(os.environ["MECABRC"])
        if d and os.path.isdir(d):
            dicdir = os.path.abspath(d)
    if dicdir is None:
        for rc in ["/usr/local/etc/mecabrc", "/etc/mecabrc"]:
            d = dicdir_from_mecabrc(rc)
            if d and os.path.isdir(d):
                dicdir = os.path.abspath(d)
                break
    if dicdir is None:
        return None, []

    setup_log.info("MeCab dictionary found in {}".format(dicdir))
    cwd = os.getcwd()
    os.chdir(dicdir)
    dicfiles = glob.glob("*")
    os.chdir(cwd)
    return dicdir, dicfiles


class build_py(_build_py):
    def _get_data_files(self):
        self.analyze_manifest()
        data_files = []
        for pkg in (self.packages or ()):
            data_files.extend(self._get_pkg_data_files(pkg))
        return data_files

    def _get_pkg_data_files(self, package):
        data = _build_py._get_pkg_data_files(self, package)
        if package == "MeCab" and USE_BUNDLED_LIBMECAB:
            d_package, d_srcdir, d_builddir, d_filenames = data
            assert d_package == package
            d_filenames.append("mecabrc.in")
            yield d_package, d_srcdir, d_builddir, d_filenames

            dicdir, dicfiles = mecab_dictionary_contents()
            if dicdir and dicfiles:
                yield (d_package, dicdir,
                       os.path.join(d_builddir, "dic"),
                       dicfiles)
        else:
            yield data


setup(name = "mecab-python3",
      description =
      "Python wrapper for the MeCab morphological analyzer for Japanese",
      long_description = read_and_trim_readme(),
      long_description_content_type = "text/markdown",
      maintainer = "Paul O'Leary McCann",
      maintainer_email = "polm@dampfkraft.com",
      url = "https://github.com/SamuraiT/mecab-python3",
      license = "BSD",
      use_scm_version=True,
      cmdclass = {
          "build_ext": build_ext,
          "build_py": build_py
      },
      package_dir = {"": "src"},
      packages = ["MeCab"],
      ext_modules = [
          Extension("MeCab._MeCab", ["src/MeCab/MeCab_wrap.cpp"])
      ],
      setup_requires = ["setuptools_scm"],
      classifiers = [
          "Development Status :: 6 - Mature",
          "Programming Language :: Python :: 2",
          "Programming Language :: Python :: 3",
          "Intended Audience :: Developers",
          "Intended Audience :: Science/Research",
          "Natural Language :: Japanese",
          "Topic :: Software Development :: Libraries :: Python Modules",
          "Topic :: Text Processing :: Linguistic",
          "License :: OSI Approved :: BSD License",
      ])
