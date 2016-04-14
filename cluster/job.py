"""
Submit jobs to slurm or torque, or with multiprocessing.

============================================================================

        AUTHOR: Michael D Dacre, mike.dacre@gmail.com
  ORGANIZATION: Stanford University
       LICENSE: MIT License, property of Stanford, use as you wish
       CREATED: 2016-44-20 23:03
 Last modified: 2016-04-14 14:38

   DESCRIPTION: Allows simple job submission with either torque, slurm, or
                with the multiprocessing module.
                To set the environement, set QUEUE to one of ['torque',
                'slurm', 'normal'], or run get_cluster_environment().
                To submit a job, run submit().

                All jobs write out a job file before submission, even though
                this is not necessary (or useful) with multiprocessing. In
                normal mode, this is a .cluster file, in slurm is is a
                .cluster.sbatch and a .cluster.script file, in torque it is a
                .cluster.qsub file.

                The name argument is required for submit, it is used to
                generate the STDOUT and STDERR files. Irrespective of mode
                the STDOUT file will be name.cluster.out and the STDERR file
                will be name.cluster.err.

                Note: `.cluster` is added to all names to make deletion less
                dangerous

                Dependency tracking is supported in torque or slurm mode,
                to use it pass a list of job ids to submit or submit_file with
                the `dependencies` keyword argument.

                To clean up cluster files, run clean(directory), if directory
                is not provided, the current directory is used.
                This will delete all files in that were generated by this
                script.

       CAUTION: The clean() function will delete **EVERY** file with
                extensions matching those in this file::
                    .cluster.err
                    .cluster.out
                    .cluster.sbatch & .cluster.script for slurm mode
                    .cluster.qsub for torque mode
                    .cluster for normal mode

============================================================================
"""
import os
import sys
import inspect
from time import sleep
from types import ModuleType
from textwrap import dedent
from subprocess import check_output, CalledProcessError
from multiprocessing import Pool, pool

# Pickle functions without defining module
import dill

###############################################################################
#                                Our functions                                #
###############################################################################

from . import run
from . import logme
from . import queue
from . import ClusterError

#########################
#  Which system to use  #
#########################

# Default is normal, change to 'slurm' or 'torque' as needed.
from . import QUEUE

##########################################################
#  The multiprocessing pool, only used in 'normal' mode  #
##########################################################

from . import POOL
from . import THREADS

# Reset broken multithreading
# Some of the numpy C libraries can break multithreading, this command
# fixes the issue.
try:
    check_output("taskset -p 0xff %d &>/dev/null" % os.getpid(), shell=True)
except CalledProcessError:
    pass  # This doesn't work on Macs or Windows

# Global Job Submission Arguments
KWARGS = ('threads', 'cores', 'time', 'mem', 'partition', 'modules',
          'dependencies', 'suffix')
ARGINFO = """\
:cores:        How many cores to run on or threads to use.
:dependencies: A list of dependencies for this job, must be either
                Job objects (required for normal mode) or job numbers.
:suffix:       The name to use in the output and error files

Used for function calls::
:imports: A list of imports, if not provided, defaults to all current
            imports, which may not work if you use complex imports.
            The list can include the import call, or just be a name, e.g
            ['from os import path', 'sys']

Used only in normal mode::
:threads:   How many threads to use in the multiprocessing pool. Defaults to
            all.

Used for torque and slurm::
:time:      The time to run for in HH:MM:SS.
:mem:       Memory to use in MB.
:partition: Partition/queue to run on, default 'normal'.
:modules:   Modules to load with the 'module load' command.
"""

###############################################################################
#                                The Job Class                                #
###############################################################################


class Job(object):

    """Information about a single job on the cluster.

    Holds information about submit time, number of cores, the job script,
    and more.

    submit() will submit the job if it is ready
    wait()   will block until the job is done
    get()    will block until the job is done and then unpickle a stored
             output (if defined) and return the contents
    clean()  will delete any files created by this object

    Printing the class will display detailed job information.

    Both wait() and get() will update the queue every two seconds and add
    queue information to the job as they go.

    If the job disappears from the queue with no information, it will be listed
    as 'complete'.

    All jobs have a .submission attribute, which is a Script object containing
    the submission script for the job and the file name, plus a 'written' bool
    that checks if the file exists.

    In addition, SLURM jobs have a .exec_script attribute, which is a Script
    object containing the shell command to run. This difference is due to the
    fact that some SLURM systems execute multiple lines of the submission file
    at the same time.

    Finally, if the job command is a function, this object will also contain a
    .function attribute, which contains the script to run the function.

    """

    id           = None
    submitted    = False
    written      = False
    done         = False

    # Holds a pool object if we are in normal mode
    pool_job     = None

    # Scripts
    submission   = None
    exec_script  = None
    function     = None

    # Dependencies
    dependencies = None

    # Holds queue information in torque and slurm
    queue_info   = None

    def __init__(self, command, args=None, name=None, path=None, **kwargs):
        """Create a job object will submission information.

        Used in all modes::
        :command:      The command or function to execute.
        :args:         Optional arguments to add to command, particularly
                       useful for functions.
        :name:         The name of the job.
        :path:         Where to create the script, if None, current dir used.
        {arginfo}
        """.format(arginfo=ARGINFO)
        # Check keyword arguments
        for arg in kwargs:
            if arg not in KWARGS:
                raise Exception('Unrecognized argument {}'.format(arg))
        # Make sure all defaults are set
        for arg in KWARGS:
            if arg not in kwargs:
                kwargs[arg] = None

        # Get environment


        # Sanitize arguments
        if not name:
            if hasattr(command, '__call__'):
                parts = str(command).strip('<>').split(' ')
                parts.remove('function')
                try:
                    parts.remove('built-in')
                except ValueError:
                    pass
                name = parts[0]
            else:
                name = command.split(' ')[0].split('/')[-1]
        name    = str(name)
        modules = kwargs['modules'] if 'modules' in kwargs else None
        modules = [modules] if isinstance(modules, str) else modules
        usedir  = os.path.abspath(path) if path else os.path.abspath('.')

        # Make sure args are a tuple or dictionary
        if args:
            if not isinstance(args, (tuple, dict)):
                if isinstance(args, list, set):
                    args = tuple(args)
                else:
                    args = (args,)

        # In case cores are passed as None
        self.cores = kwargs['cores'] if 'cores' in kwargs else 1

        # Set output files
        suffix = kwargs['suffix'] if kwargs['suffix'] else 'cluster'
        self.outfile = '.'.join([name, suffix, 'out'])
        self.errfile = '.'.join([name, suffix, 'err'])

        # Check and set dependencies
        if kwargs['dependencies']:
            dependencies = kwargs['dependencies']
            self.dependencies = []
            if isinstance(dependencies, 'str'):
                if not dependencies.isdigit():
                    raise ClusterError('Dependencies must be number or list')
                else:
                    dependencies = [int(dependencies)]
            elif isinstance(dependencies, (int, Job)):
                dependencies = [dependencies]
            elif not isinstance(dependencies, (tuple, list)):
                raise ClusterError('Dependencies must be number or list')
            for dependency in dependencies:
                if isinstance(dependency, str):
                    dependency  = int(dependency)
                if not isinstance(dependency, (int, Job)):
                    raise ClusterError('Dependencies must be number or list')
                self.dependencies.append(dependency)

        # Make functions run remotely
        if hasattr(command, '__call__'):
            self.function = Function(
                file_name=os.path.join(usedir, name + '_func.py'),
                function=command, args=args)
            command = 'python{} {}'.format(sys.version[0],
                                           self.function.file_name)
            args = None

        # Collapse args into command
        command = command + ' '.join(args) if args else command

        # Build execution wrapper with modules
        precmd  = ''
        if kwargs['modules']:
            for module in kwargs['modules']:
                precmd += 'module load {}\n'.format(module)
        precmd += dedent("""\
            cd {}
            date +'%d-%H:%M:%S'
            echo "Running {}"
            """.format(usedir, name))
        pstcmd = dedent("""\
            exitcode=$?
            echo Done
            date +'%d-%H:%M:%S'
            if [[ $exitcode != 0 ]]; then
                echo Exited with code: $exitcode >&2
            fi
            """)

        # Create queue-dependent scripts
        sub_script = []
        if QUEUE == 'slurm':
            self.qtype = 'slurm'
            scrpt = os.path.join(usedir, '{}.cluster.sbatch'.format(name))
            sub_script.append('#!/bin/bash')
            if 'partition' in kwargs:
                sub_script.append('#SBATCH -p {}'.format(kwargs['partition']))
            sub_script.append('#SBATCH --ntasks 1')
            sub_script.append('#SBATCH --cpus-per-task {}'.format(self.cores))
            if 'time' in kwargs:
                sub_script.append('#SBATCH --time={}'.format(kwargs['time']))
            if 'mem' in kwargs:
                sub_script.append('#SBATCH --mem={}'.format(kwargs['mem']))
            sub_script.append('#SBATCH -o {}'.format(self.outfile))
            sub_script.append('#SBATCH -e {}'.format(self.errfile))
            sub_script.append('cd {}'.format(usedir))
            sub_script.append('srun bash {}.script'.format(
                os.path.join(usedir, name)))
            exe_scrpt  = os.path.join(usedir, name + '.script')
            exe_script = []
            exe_script.append('#!/bin/bash')
            exe_script.append('mkdir -p $LOCAL_SCRATCH')
            exe_script.append(precmd)
            exe_script.append(command + '\n')
            exe_script.append(pstcmd)
        elif QUEUE == 'torque':
            self.qtype = 'torque'
            scrpt = os.path.join(usedir, '{}.cluster.qsub'.format(name))
            sub_script.append('#!/bin/bash')
            if 'partition' in kwargs:
                sub_script.append('#PBS -q {}'.format(kwargs['partition']))
            sub_script.append('#PBS -l nodes=1:ppn={}'.format(self.cores))
            if 'time' in kwargs:
                sub_script.append('#PBS -l walltime={}'.format(kwargs['time']))
            if 'mem' in kwargs:
                sub_script.append('#PBS mem={}MB'.format(kwargs['mem']))
            sub_script.append('#PBS -o {}.cluster.out'.format(name))
            sub_script.append('#PBS -e {}.cluster.err\n'.format(name))
            sub_script.append('mkdir -p $LOCAL_SCRATCH')
            sub_script.append(precmd)
            sub_script.append(command + '')
            sub_script.append(pstcmd)
        elif QUEUE == 'normal':
            self.qtype = 'normal'
            # Create the pool
            global POOL
            if not POOL or POOL._state != 0:
                threads = kwargs['threads'] if 'threads' in kwargs else THREADS
                POOL = Pool(threads)
            scrpt = os.path.join(usedir, '{}.cluster'.format(name))
            sub_script.append('#!/bin/bash\n')
            sub_script.append(precmd)
            sub_script.append(command + '\n')
            sub_script.append(pstcmd)

        # Create the Script objects
        self.submission = Script(script='\n'.join(sub_script),
                                 file_name=scrpt)
        if self.exe_scrpt:
            self.exec_script = Script(script='\n'.join(exe_script),
                                      file_name=exe_scrpt)

    ####################
    #  Public Methods  #
    ####################

    def write(self, overwrite=True):
        """Write all scripts."""
        self.submission.write(overwrite)
        if self.exec_script:
            self.exec_script.write(overwrite)
        if self.function:
            self.function.write(overwrite)
        self.written = True

    def clean(self):
        """Delete all scripts created by this module, if they were written."""
        for jobfile in [self.submission, self.exec_script, self.function]:
            if jobfile:
                jobfile.clean()

    def submit(self, max_queue_len=None):
        """Submit this job.

        If max_queue_len is specified (or in defaults), then this method will
        block until the queue is open enough to allow submission.

        NOTE: In normal mode, dependencies will result in this function blocking
              until the dependencies are satisfied, not idea behavior.

        Returns self.
        """
        if not self.written:
            self.write()
        if self.qtype == 'normal':
            if self.dependencies:
                for depend in self.dependencies:
                    if not isinstance(depend, (Job, pool.ApplyResult)):
                        raise Exception('In normal mode, dependency tracking' +
                                        'only works with Job objects.')
                    # Block until tasks are done
                    if not depend.done:
                        depend.wait()
            global POOL
            if not POOL or POOL._state != 0:
                POOL = Pool(THREADS)
            command = 'bash {}'.format(self.submission.file_name)
            args = dict(stdout=self.stdout,
                        stderr=self.stderr)
            self.pool_job = POOL.apply_async(run.cmd, (command,), args)
            self.submitted = True
            return self
        elif self.qtype == 'slurm':
            if self.dependencies:
                dependencies = []
                for depend in self.dependencies:
                    if isinstance(depend, Job):
                        dependencies.append(str(depend.id))
                    else:
                        dependencies.append(str(depend))
                    depends = '--dependency=afterok:{}'.format(
                        ':'.join(dependencies))
                    args = ['sbatch', depends, self.submission.file_name]
            else:
                args = ['sbatch', self.submission.file_name]
            # Try to submit job 5 times
            count = 0
            while True:
                try:
                    self.id = int(check_output(args).decode().rstrip().split(' ')[-1])
                except CalledProcessError:
                    if count == 5:
                        raise
                    count += 1
                    sleep(1)
                    continue
                break
            self.submitted = True
            return self

        elif self.qtype == 'torque':
            if self.dependencies:
                dependencies = []
                for depend in self.dependencies:
                    if isinstance(depend, Job):
                        dependencies.append(str(depend.id))
                    else:
                        dependencies.append(str(depend))
                depends = '-W depend={}'.format(
                    ','.join(['afterok:' + d for d in dependencies]))
                args = ['qsub', depends, self.submission.file_name]
            else:
                args = ['qsub', self.submission.file_name]
            # Try to submit job 5 times
            count = 0
            while True:
                try:
                    self.id = int(check_output(args).decode().rstrip().split('.')[0])
                except CalledProcessError:
                    if count == 5:
                        raise
                    count += 1
                    sleep(1)
                    continue
                break
            self.submitted = True
            return self

    def wait(self):
        """Block until job completes."""
        if self.qtype == 'normal' and self.pool_job:
            self.pool_job.wait()
        else:
            job_list = queue.Queue()
            job_list.wait(self)
            self.queue_info = job_list[self.id]
            assert self.id == self.queue_info.id
        self.done = True

    def get(self):
        """Block until job completed and then return exit_code, stdout, stderr."""
        self.wait()
        if self.qtype == 'normal' and self.pool_job:
            return self.pool_job.get()
        else:
            try:
                with open(self.outfile, 'r') as fin:
                    outstr = fin.read()
            except OSError:
                outstr = None
            try:
                with open(self.errfile, 'r') as fin:
                    errstr = fin.read()
            except OSError:
                errstr = None
            return self.queue_info.exitcode, outstr, errstr

    ###############
    #  Internals  #
    ###############

    def __getattr__(self, key):
        """Handle dynamic attributes."""
        if key == 'files':
            files = [self.submission]
            if self.exec_script:
                files.append(self.exec_script)
            if self.function:
                files.append(self.function)
            return files

    def __repr__(self):
        """Return simple job information."""
        if self.submitted:
            outstr = "Job<{id}".format(id=self.id)
        else:
            outstr = "Job<NOT_SUBMITTED"
        outstr += "({name};command:{cmnd};args:{args};qtype={qtype})".format(
            name=self.name, cmnd=self.command, args=self.args, qtype=self.qtype)
        if self.done:
            outstr += "COMPLETED"
        elif self.written:
            outstr += "WRITTEN"
        return outstr

    def __str__(self):
        """Print job name and ID + status."""
        if self.done:
            state = 'complete'
        elif self.written:
            state = 'written'
        else:
            state = 'not written'
        return "{name} ID: {id}, state: {state}".format(
            name=self.name, id=self.id, state=state)


class Script(object):

    """A script string plus a file name."""

    written = False

    def __init__(self, file_name, script):
        """Initialize the script and file name."""
        self.script    = script
        self.file_name = os.path.abspath(file_name)

    def write(self, overwrite=True):
        """Write the script file."""
        if overwrite or not os.path.exists(self.file_name):
            with open(self.file_name, 'w') as fout:
                fout.write(self.script + '\n')
            self.written = True
            return self.file_name
        else:
            return None

    def clean(self):
        """Delete any files made by us."""
        if self.written and self.exists:
            os.remove(self.file_name)

    def __getattr__(self, attr):
        """Make sure boolean is up to date."""
        if attr == 'exists':
            return os.path.exists(self.file_name)

    def __repr__(self):
        """Display simple info."""
        return "Script<{}(exists: {}; written: {})>".format(
            self.file_name, self.exists, self.written)

    def __str__(self):
        """Print the script."""
        return repr(self) + '::\n\n' + self.script + '\n'


class Function(Script):

    """A special Script used to run a function."""

    def __init__(self, file_name, function, args=None, imports=None,
                 pickle_file=None, outfile=None):
        """Create a function wrapper.

        :function:    Function handle.
        :args:        Arguments to the function as a tuple.
        :imports:     A list of imports, if not provided, defaults to all current
                      imports, which may not work if you use complex imports.
                      The list can include the import call, or just be a name, e.g
                      ['from os import path', 'sys']
        :pickle_file: The file to hold the function.
        :outfile:     The file to hold the output.
        :path:        The path to the calling script, used for importing self.
        """
        self.function = function
        self.parent   = inspect.getmodule(self.function).__name__
        self.args     = args
        # Get the module path
        rootmod  = inspect.getmodule(function)
        imppath  = os.path.split(rootmod.__file__)[0]
        rootname = rootmod.__name__

        script = '#!/usr/bin/env python{}\n'.format(sys.version[0])
        if imports:
            if not isinstance(imports, (list, tuple)):
                imports = [imports]
        else:
            imports = []
            for module in globals().values():
                if isinstance(module, ModuleType):
                    imports.append(module.__name__)
            imports = list(set(imports))

        filtered_imports = []
        for imp in imports:
            if imp.startswith('import') or imp.startswith('from'):
                filtered_imports.append(imp.rstrip())
            else:
                if '.' in imp:
                    rootimp = imp.split('.')[0]
                    if not rootimp == 'dill' and not rootimp == 'sys':
                        filtered_imports.append('import {}'.format(rootimp))
                if imp == 'dill' or imp == 'sys':
                    continue
                filtered_imports.append('import {}'.format(imp))
        # Get rid of duplicates and sort imports
        script += '\n'.join(sorted(set(filtered_imports)))

        # Set file names
        self.pickle_file = pickle_file if pickle_file else file_name + '.pickle.in'
        self.outfile     = outfile if outfile else file_name + '.pickle.out'

        # Create script text
        script += '\n\n' + run.FUNC_RUNNER.format(path=imppath,
                                                  module=rootname,
                                                  pickle_file=self.pickle_file,
                                                  out_file=self.outfile)

        super(Function, self).__init__(file_name, script)

    def write(self, overwrite=True):
        """Write the pickle file and call the parent Script write function."""
        with open(self.pickle_file, 'wb') as fout:
            dill.dump((self.function, self.args), fout)
        super(Function, self).write(overwrite)

    def clean(self):
        """Delete any files made by us."""
        if self.written:
            if os.path.isfile(self.pickle_file):
                os.remove(self.pickle_file)
            if os.path.isfile(self.outfile):
                os.remove(self.outfile)
        super(Function, self).clean()


###############################################################################
#                            Submission Functions                             #
###############################################################################


def submit(command, args=None, name=None, path=None, **kwargs):
    """Submit a script to the cluster.

    Used in all modes::
    :command:   The command or function to execute.
    :args:      Optional arguments to add to command, particularly
                useful for functions.
    :name:      The name of the job.
    :path:      Where to create the script, if None, current dir used.

    {arginfo}

    Returns:
        Job object
    """.format(arginfo=ARGINFO)
    # Check keyword arguments
    for arg in kwargs:
        if arg not in KWARGS:
            raise Exception('Unrecognized argument {}'.format(arg))

    queue.check_queue()  # Make sure the QUEUE is usable

    job = Job(command=command, args=args, name=name, path=path, **kwargs)

    job.write()
    job.submit()

    return job


#########################
#  Job file generation  #
#########################


def make_job(command, args=None, name=None, path=None, **kwargs):
    """Make a job file compatible with the chosen cluster.

    If mode is normal, this is just a simple shell script.

     Used in all modes::
    :command:   The command or function to execute.
    :args:      Optional arguments to add to command, particularly
                useful for functions.
    :name:      The name of the job.
    :path:      Where to create the script, if None, current dir used.

    {arginfo}

    Returns:
        A job object
    """.format(arginfo=ARGINFO)
    # Check keyword arguments
    for arg in kwargs:
        if arg not in KWARGS:
            raise Exception('Unrecognized argument {}'.format(arg))

    queue.check_queue()  # Make sure the QUEUE is usable

    job = Job(command=command, args=args, name=name, path=path, **kwargs)

    # Return the path to the script
    return job


def make_job_file(command, args=None, name=None, path=None, **kwargs):
    """Make a job file compatible with the chosen cluster.

    If mode is normal, this is just a simple shell script.

     Used in all modes::
    :command:   The command or function to execute.
    :args:      Optional arguments to add to command, particularly
                useful for functions.
    :name:      The name of the job.
    :path:      Where to create the script, if None, current dir used.

    {arginfo}

    Returns:
        Path to job script
    """.format(arginfo=ARGINFO)
    # Check keyword arguments
    for arg in kwargs:
        if arg not in KWARGS:
            raise Exception('Unrecognized argument {}'.format(arg))

    queue.check_queue()  # Make sure the QUEUE is usable

    job = Job(command=command, args=args, name=name, path=path, **kwargs)

    job = job.write()

    # Return the path to the script
    return job.submission


##############
#  Cleaning  #
##############


def clean(jobs):
    """Delete all files in jobs list or single Job object."""
    if isinstance(jobs, Job):
        jobs = [jobs]
    if not isinstance(jobs, (list, tuple)):
        raise ClusterError('Job list must be a Job, list, or tuple')
    for job in jobs:
        job.clean()


###############################################################################
#                      Job Object Independent Functions                       #
###############################################################################


def submit_file(script_file, name=None, dependencies=None, threads=None):
    """Submit a job file to the cluster.

    If QUEUE is torque, qsub is used; if QUEUE is slurm, sbatch is used;
    if QUEUE is normal, the file is executed with subprocess.

    This function is independent of the job object and just submits a file.

    :dependencies: A job number or list of job numbers.
                   In slurm: `--dependency=afterok:` is used
                   For torque: `-W depend=afterok:` is used

    :threads:      Total number of threads to use at a time, defaults to all.
                   ONLY USED IN NORMAL MODE

    :name:         The name of the job, only used in normal mode.

    :returns:      job number for torque or slurm
                   multiprocessing job object for normal mode
    """
    queue.check_queue()  # Make sure the QUEUE is usable

    # Sanitize arguments
    name = str(name)

    # Check dependencies
    if dependencies:
        if isinstance(dependencies, (str, int)):
            dependencies = [dependencies]
        if not isinstance(dependencies, (list, tuple)):
            raise Exception('dependencies must be a list, int, or string.')
        dependencies = [str(i) for i in dependencies]

    if QUEUE == 'slurm':
        if dependencies:
            dependencies = '--dependency=afterok:{}'.format(
                ':'.join([str(d) for d in dependencies]))
            args = ['sbatch', dependencies, script_file]
        else:
            args = ['sbatch', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split(' ')[-1])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'torque':
        if dependencies:
            dependencies = '-W depend={}'.format(
                ','.join(['afterok:' + d for d in dependencies]))
            args = ['qsub', dependencies, script_file]
        else:
            args = ['qsub', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split('.')[0])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'normal':
        global POOL
        if not POOL:
            POOL = Pool(threads) if threads else Pool()
        command = 'bash {}'.format(script_file)
        args = dict(stdout=name + '.cluster.out', stderr=name + '.cluster.err')
        return POOL.apply_async(run.cmd, (command,), args)


def clean_dir(directory='.', suffix='cluster'):
    """Delete all files made by this module in directory.

    CAUTION: The clean() function will delete **EVERY** file with
             extensions matching those in this file::
                 .cluster.err
                 .cluster.out
                 .cluster.sbatch & .cluster.script for slurm mode
                 .cluster.qsub for torque mode
                 .cluster for normal mode

    :directory: The directory to run in, defaults to the current directory.
    :returns:   A set of deleted files
    """
    queue.check_queue()  # Make sure the QUEUE is usable

    extensions = ['.' + suffix + '.err', '.' + suffix + '.out']
    if QUEUE == 'normal':
        extensions.append('.' + suffix)
    elif QUEUE == 'slurm':
        extensions = extensions + ['.' + suffix + '.sbatch',
                                   '.' + suffix + '.script']
    elif QUEUE == 'torque':
        extensions.append('.' + suffix + '.qsub')

    files = [i for i in os.listdir(os.path.abspath(directory))
             if os.path.isfile(i)]

    if not files:
        logme.log('No files found.', 'debug')
        return []

    deleted = []
    for f in files:
        for extension in extensions:
            if f.endswith(extension):
                os.remove(f)
                deleted.append(f)

    return deleted