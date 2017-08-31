##
#  @file tests.py
#
#  Control which tests run, and how.
##
from __future__ import print_function, absolute_import
import glob
import os
import sys
import pipes
from past.builtins import basestring
from SCons.Script import *  # noqa F403 F401 So that this file has the same namespace as SConstruct/SConscript
from . import state
from . import utils


##
#  @brief A class to control unit tests.
#
#  This class is unchanged from previous versions of sconsUtils, but it will now generally
#  be called via scripts.BasicSConscript.tests().
##
class Control(object):
    _IGNORE = "IGNORE"
    _EXPECT_FAILURE = "EXPECT_FAILURE"

    ##
    #  @brief Create an object to run tests
    #
    #  @param env           An SCons Environment (almost always lsst.sconsUtils.env).
    #  @param ignoreList    A list of tests that should NOT be run --- useful in conjunction
    #                       with glob patterns.  If a file is listed as "@fileName", the @ is stripped and
    #                       we don't bother to check if fileName exists (useful for machine-generated files).
    #  @param expectedFalures   A dictionary; the keys are tests that are known to fail; the values
    #                           are strings to print.
    #  @param args          A dictionary with testnames as keys, and argument strings as values.
    #                       As scons always runs from the top-level directory, tests has to fiddle with
    #                       paths.  If an argument is a file this is done automatically; if it's e.g.
    #                       just a basename then you have to tell tests that it's really (part of a)
    #                       filename by prefixing the name by "file:".
    #
    #  @param tmpDir        The location of the test outputs.
    #  @param verbose       How chatty you want the test code to be.
    #
    #  @code
    #  tests = lsst.tests.Control(
    #      env,
    #      args={
    #           "MaskIO_1" :      "data/871034p_1_MI_msk.fits",
    #           "MaskedImage_1" : "file:data/871034p_1_MI foo",
    #      },
    #      ignoreList=["Measure_1"],
    #      expectedFailures={"BBox_1": "Problem with single-pixel BBox"}
    # )
    # @endcode
    ##
    def __init__(self, env, ignoreList=None, expectedFailures=None, args=None,
                 tmpDir=".tests", verbose=False):
        if 'PYTHONPATH' in os.environ:
            env.AppendENVPath('PYTHONPATH', os.environ['PYTHONPATH'])

        self._env = env

        self._tmpDir = tmpDir
        self._cwd = os.path.abspath(os.path.curdir)

        # Calculate the absolute path for temp dir if it is relative.
        # This assumes the temp dir is relative to where the tests SConscript
        # file is located. SCons will know how to handle this itself but
        # some options require the code to know where to write things.
        if os.path.isabs(self._tmpDir):
            self._tmpDirAbs = self._tmpDir
        else:
            self._tmpDirAbs = os.path.join(self._cwd, self._tmpDir)

        self._verbose = verbose

        self._info = {}                 # information about processing targets
        if ignoreList:
            for f in ignoreList:
                if f.startswith("@"):  # @dfilename => don't complain if filename doesn't exist
                    f = f[1:]
                else:
                    if not os.path.exists(f):
                        state.log.warn("You're ignoring a non-existent file, %s" % f)
                self._info[f] = (self._IGNORE, None)

        if expectedFailures:
            for f in expectedFailures:
                self._info[f] = (self._EXPECT_FAILURE, expectedFailures[f])

        if args:
            self._args = args           # arguments for tests
        else:
            self._args = {}

        self.runExamples = True                      # should I run the examples?
        try:
            # file is user read/write/executable
            self.runExamples = (os.stat(self._tmpDir).st_mode & 0o700) != 0
        except OSError:
            pass

        if not self.runExamples:
            print("Not running examples; \"chmod 755 %s\" to run them again" % self._tmpDir,
                  file=sys.stderr)

    def args(self, test):
        try:
            return self._args[test]
        except KeyError:
            return ""

    def ignore(self, test):
        if not test.endswith(".py") and \
           len(self._env.Glob(test)) == 0:  # we don't know how to build it
            return True

        ignoreFile = test in self._info and self._info[test][0] == self._IGNORE

        if self._verbose and ignoreFile:
            print("Skipping", test, file=sys.stderr)

        return ignoreFile

    def messages(self, test):
        """Return the messages to be used in case of success/failure; the logicals
        (note that they are strings) tell whether the test is expected to pass"""

        if test in self._info and self._info[test][0] == self._EXPECT_FAILURE:
            msg = self._info[test][1]
            return ("false", "Passed, but should have failed: %s" % msg,
                    "true", "Failed as expected: %s" % msg)
        else:
            return ("true", "passed",
                    "false", "failed")

    def run(self, fileGlob):
        """Create a test target for each file matching the supplied glob.
        """

        if not isinstance(fileGlob, basestring):  # env.Glob() returns an scons Node
            fileGlob = str(fileGlob)
        targets = []
        if not self.runExamples:
            return targets

        # Determine any library load path values that we have to prepend
        # to the command.
        libpathstr = utils.libraryLoaderEnvironment()

        for f in glob.glob(fileGlob):
            interpreter = ""            # interpreter to run test, if needed

            if f.endswith(".cc"):  # look for executable
                f = os.path.splitext(f)[0]
            else:
                interpreter = "pytest -Wd --junit-xml=${TARGET}.xml"
                interpreter += " --junit-prefix={0}".format(self.junitPrefix())
                interpreter += self._pytestCoverage(prefix="${TARGET}")

            if self.ignore(f):
                continue

            target = os.path.join(self._tmpDir, f)

            args = []
            for a in self.args(f).split(" "):
                # if a is a file, make it an absolute name as scons runs from the root directory
                filePrefix = "file:"
                if a.startswith(filePrefix):  # they explicitly said that this was a file
                    a = os.path.join(self._cwd, a[len(filePrefix):])
                else:
                    try:                # see if it's a file
                        os.stat(a)
                        a = os.path.join(self._cwd, a)
                    except OSError:
                        pass

                args += [a]

            (should_pass, passedMsg, should_fail, failedMsg) = self.messages(f)

            # The TRAVIS environment variable is set to allow us to disable
            # the matplotlib font cache. See ticket DM-3856.
            # TODO: Work out better way of solving matplotlib issue in build.
            expandedArgs = " ".join(args)
            result = self._env.Command(target, f, """
            @rm -f ${TARGET}.failed;
            @printf "%%s" 'running ${SOURCES}... ';
            @echo $SOURCES %s > $TARGET; echo >> $TARGET;
            @if %s TRAVIS=1 %s $SOURCES %s >> $TARGET 2>&1; then \
               if ! %s; then mv $TARGET ${TARGET}.failed; fi; \
               echo "%s"; \
            else \
               if ! %s; then mv $TARGET ${TARGET}.failed; fi; \
               echo "%s"; \
            fi;
            """ % (expandedArgs, libpathstr, interpreter, expandedArgs, should_pass,
                   passedMsg, should_fail, failedMsg))

            targets.extend(result)

            self._env.Alias(os.path.basename(target), target)

            self._env.Clean(target, self._tmpDir)

        return targets

    def runPythonTests(self, pyList):
        """Add a single target for testing all python files. pyList is
        a list of nodes corresponding to python test files. The
        IgnoreList is respected when scanning for entries. If pyList
        is None, or an empty list, it uses automated test discovery
        within pytest. This differs from the behavior of scripts.tests()
        where a distinction is made. Returns a list containing a single
        target."""

        if pyList is None:
            pyList = []

        # Determine any library load path values that we have to prepend
        # to the command.
        libpathstr = utils.libraryLoaderEnvironment()

        # Get list of python files with the path included.
        pythonTestFiles = []
        for fileGlob in pyList:
            if not isinstance(fileGlob, basestring):  # env.Glob() returns an scons Node
                fileGlob = str(fileGlob)
            for f in glob.glob(fileGlob):
                if self.ignore(f):
                    continue
                pythonTestFiles.append(os.path.join(self._cwd, f))

        # Now set up the python testing target
        # We always want to run this with the tests target.
        # We have decided to use pytest caching so that on reruns we only
        # run failed tests.
        interpreter = "pytest -Wd --lf --junit-xml=${TARGET} --session2file=${TARGET}.out"
        interpreter += " --junit-prefix={0}".format(self.junitPrefix())
        interpreter += self._pytestCoverage()

        target = os.path.join(self._tmpDir, "pytest-{}.xml".format(self._env['eupsProduct']))

        # Work out how many jobs scons has been configured to use
        # and use that number with pytest. This could cause trouble
        # if there are lots of binary tests to run and lots of singles.
        njobs = self._env.GetOption("num_jobs")
        print("Running pytest with {} process{}".format(njobs, "" if njobs == 1 else "es"))
        if njobs > 1:
            interpreter = interpreter + " -n {}".format(njobs)

        # Remove target so that we always trigger pytest
        if os.path.exists(target):
            os.unlink(target)

        if not pythonTestFiles:
            print("pytest: automated test discovery mode enabled.")
        else:
            nfiles = len(pythonTestFiles)
            print("pytest: running on {} Python test file{}.".format(nfiles, "" if nfiles == 1 else "s"))

        result = self._env.Command(target, None, """
        @rm -f ${{TARGET}} ${{TARGET}}.failed;
        @printf "%s\\n" 'running global pytest... ';
        @if {2} TRAVIS=1 {0} {1}; then \
            echo "Global pytest run completed successfully"; \
        else \
            echo "Global pytest run: failed"; \
            mv ${{TARGET}}.out ${{TARGET}}.failed; \
        fi;
        """.format(interpreter, " ".join([pipes.quote(p) for p in pythonTestFiles]), libpathstr))

        self._env.Alias(os.path.basename(target), target)
        self._env.Clean(target, self._tmpDir)

        return [result]

    def junitPrefix(self):
        controlVar = "LSST_JUNIT_PREFIX"
        prefix = self._env['eupsProduct']

        if controlVar in os.environ:
            prefix += ".{0}".format(os.environ[controlVar])

        return prefix

    def _pytestCoverage(self, prefix="pytest"):
        """Form the additional arguments required to enable coverage testing.
        Prefix is used to form the output files.
        """

        options = ""

        # if we have a standard package layout, restrict coverage to the
        # python code and do not include the tests.
        if os.path.exists("python") or os.path.exists(os.path.join(os.path.pardir, "python")):
            options += " --cov=python"
        else:
            # Default to coverage for everything in this directory and below
            options += " --cov=."

        options += " --cov-branch --cov-report=term "

        covfile = "{}-cov-{}.xml".format(prefix, self._env['eupsProduct'])

        # We should specify the output directory explicitly unless the prefix
        # indicates that we are using the SCons target
        if covfile.startswith("${TARGET}"):
            covpath = covfile
        else:
            covpath = os.path.join(self._tmpDirAbs, covfile)
        options += " --cov-report=xml:'{}'".format(covpath)

        # Use a prefix for the HTML if the prefix is not the default
        htmlfile = ""
        if prefix != "pytest":
            htmlfile = ":'htmlcov-{}'".format(prefix)
        options += " --cov-report=html{}".format(htmlfile)
        print("Coverage options: ", options)
        return options
