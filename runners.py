# MIT License
#
# Copyright (c) 2020-2023 Tim Niklas Uhl
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from expcore import ExperimentSuite
import expcore
from pathlib import Path
import subprocess, sys, json, os
import math as m
from string import Template
import time
from datetime import date

def format_duration(seconds):
    days, remainder = divmod(seconds, 3600*24)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    formatted = f"{days}-{hours:02}:{minutes:02}:{seconds:02}"
    return formatted


class BaseRunner:
    def __init__(
        self,
        suite_name,
        experiment_data_directory,
        machine,
        output_directory,
        command_template,
        omit_json_output_path=False,
        omit_seed=False,
    ):
        # append experiment_data_dir with current date
        data_suffix = date.today().strftime("%y_%m_%d")
        self.experiment_data_directory = Path(experiment_data_directory) / (
            suite_name + "_" + data_suffix
        )
        self.experiment_data_directory.mkdir(exist_ok=True, parents=True)
        self.machine = machine
        self.output_directory = (
            Path(output_directory)
            if output_directory
            else (self.experiment_data_directory / "output")
        )
        self.output_directory.mkdir(exist_ok=True, parents=True)
        if not command_template:
            command_template = self.default_command_template()
        self.command_template = command_template
        self.omit_json_output_path = omit_json_output_path
        self.omit_seed = omit_seed
        self.tasks_per_node = None

    def make_cmd_for_config(
        self,
        suite: ExperimentSuite,
        input,
        config_job_name,
        config_index,
        mpi_ranks,
        threads_per_rank,
        seed,
        config,
    ):
        json_output_prefix_path = (
            self.output_directory / f"{config_job_name}_timer.json"
        )
        config = config.copy()
        if not self.omit_json_output_path:
            config["json_output_path"] = str(json_output_prefix_path)
        if not self.omit_seed:
            config["seed"] = seed
        cmd = expcore.command(
            suite.executable,
            ".",
            input,
            mpi_ranks,
            threads_per_rank,
            escape=True,
            **config,
        )
        return cmd

    def execute(self, experiment_suite: ExperimentSuite):
        raise NotImplementedError("Please implement this method.")

    def default_command_template(self):
        raise RuntimeError("No default for command template, please provide one")

sbatch_template_dir = Path(__file__).parent / "sbatch-templates"
command_template_dir = Path(__file__).parent / "command-templates"

class SharedMemoryRunner(BaseRunner):

    def default_command_template(self):
        return command_template_dir / "command_template_shared.txt"

    def __init__(
        self,
        suite_name,
        max_cores: int,
        experiment_data_directory,
        output_directory,
        command_template,
        omit_json_output_path,
        omit_seed
    ):
        BaseRunner.__init__(self, suite_name, experiment_data_directory, "shared", output_directory, command_template, omit_json_output_path, omit_seed)
        self.max_cores = max_cores
        self.failed = 0
        self.total_jobs = 0

    def execute(self, experiment_suite: ExperimentSuite):
        print(f"Running suite {experiment_suite.name} ...")
        with open(self.output_directory / "config.json", "w") as file:
            json.dump(experiment_suite.configs, file, indent=4)
        with open(self.command_template) as template_file:
            command_template = template_file.read()
        command_template = Template(command_template)
        for input in experiment_suite.inputs:
            for i, config in enumerate(experiment_suite.configs):
                for ncores in experiment_suite.cores:
                    if ncores > self.max_cores:
                        continue
                    for seed in experiment_suite.seeds:
                        for threads in experiment_suite.threads_per_rank:
                            local_config = config.copy()
                            mpi_ranks = ncores // threads
                            if isinstance(input, expcore.InputGraph):
                                input_name = input.name
                            else:
                                input_name = str(input)
                            jobname = f"{input_name}-np{mpi_ranks}-t{threads}"
                            config_job_name = jobname + "-c" + str(i) + "-s" + str(seed)
                            json_output_prefix_path = (
                                self.output_directory / f"{config_job_name}_timer.json"
                            )
                            local_config["json_output_path"] = str(json_output_prefix_path)
                            log_path = self.output_directory / f"{config_job_name}-log.txt"
                            err_path = self.output_directory / f"{config_job_name}-error-log.txt"

                            cmd = self.make_cmd_for_config(
                                experiment_suite,
                                input,
                                config_job_name,
                                i,
                                mpi_ranks,
                                threads,
                                seed,
                                config
                            )
                            cmd_string = command_template.substitute(cmd=' '.join(cmd), mpi_ranks=mpi_ranks)
                            print(
                                f"Running config {i} on {input_name} using {mpi_ranks} ranks and {threads} threads per rank ... ",
                            )
                            print(cmd_string, end="")
                            sys.stdout.flush()
                            with open(log_path, "w") as log_file:
                                with open(err_path, "w") as err_file:
                                    ret = subprocess.run(
                                        cmd_string, stdout=log_file, stderr=err_file, shell=True
                                    )
                            if ret.returncode == 0:
                                print("finished.")
                            else:
                                self.failed += 1
                                print("failed.")
                            self.total_jobs += 1
        print(f"Finished suite {experiment_suite.name}. Output files in {self.output_directory}")
        print(
            f"Summary: {self.failed} out of {self.total_jobs} failed."
        )

class SBatchRunner(BaseRunner):

    def default_sbatch_template(self):
        raise RuntimeError("No default for command template, please provide one")

    def __init__(
        self,
        suite_name,
        experiment_data_directory,
        machine,
        output_directory,
        job_output_directory,
        sbatch_template,
        command_template,
        module_config,
        module_restore_cmd,
        time_limit,
        use_test_partition=False,
        omit_json_output_path=False,
        omit_seed=False,
    ):
        BaseRunner.__init__(
            self,
            suite_name,
            experiment_data_directory,
            machine,
            output_directory,
            command_template,
            omit_json_output_path,
            omit_seed
        )
        self.job_output_directory = (
            Path(job_output_directory)
            if job_output_directory
            else (self.experiment_data_directory / "jobfiles")
        )
        self.job_output_directory.mkdir(exist_ok=True, parents=True)

        self.module_config = module_config
        self.module_restore_cmd = module_restore_cmd
        self.time_limit = time_limit
        self.use_test_partition = use_test_partition
        if not sbatch_template:
            sbatch_template = self.default_sbatch_template()
        self.sbatch_template = sbatch_template
        
    def execute(self, experiment_suite: ExperimentSuite):
        project = os.environ.get("PROJECT", "PROJECT_NOT_SET")
        with open(self.output_directory / "config.json", "w") as file:
            json.dump(experiment_suite.configs, file, indent=4)
        with open(self.sbatch_template) as template_file:
            template = template_file.read()
        template = Template(template)
        with open(self.command_template) as template_file:
            command_template = template_file.read()
        command_template = Template(command_template)
        njobs = 0
        for input in experiment_suite.inputs:
            if isinstance(input, expcore.InputGraph):
                input_name = input.name
            else:
                input_name = str(input)
            for ncores in experiment_suite.cores:
                if experiment_suite.tasks_per_node:
                    tasks_per_node = experiment_suite.tasks_per_node
                else:
                    tasks_per_node = self.tasks_per_node

                aggregate_jobname = (
                    f"{experiment_suite.name}-{input_name}-cores{ncores}"
                )
                log_path = self.output_directory / f"{input_name}-cores{ncores}-log.txt"
                err_log_path = (
                    self.output_directory / f"{input_name}-cores{ncores}-error-log.txt"
                )
                subs = {}
                nodes = self.required_nodes(ncores, tasks_per_node)
                subs["nodes"] = nodes
                subs["ntasks"] = ncores
                subs["ntasks_per_node"] = tasks_per_node
                subs["output_log"] = str(log_path)
                subs["error_output_log"] = str(err_log_path)
                subs["job_name"] = aggregate_jobname
                subs["job_queue"] = self.get_queue(
                    ncores, tasks_per_node, self.use_test_partition
                )
                subs["islands"] = self.required_islands(nodes)
                subs["account"] = project
                if self.module_config:
                    subs["module_setup"] = f"{self.module_restore_cmd} {self.module_config}"
                else:
                    subs["module_setup"] = "# no specific module setup given"
                time_limit = 0
                commands = []
                for threads_per_rank in experiment_suite.threads_per_rank:
                    mpi_ranks = ncores // threads_per_rank
                    ranks_per_node = tasks_per_node // threads_per_rank
                    jobname = f"{input_name}-np{mpi_ranks}-t{threads_per_rank}"
                    for i, config in enumerate(experiment_suite.configs):
                        for seed in experiment_suite.seeds:
                            job_time_limit = experiment_suite.get_input_time_limit(
                                input.name
                            )
                            if not job_time_limit:
                                job_time_limit = self.time_limit
                            time_limit += job_time_limit
                            config_jobname = jobname + "-c" + str(i) + "-s" + str(seed)
                            cmd = self.make_cmd_for_config(
                                experiment_suite,
                                input,
                                config_jobname,
                                i,
                                mpi_ranks,
                                threads_per_rank,
                                seed,
                                config,
                            )
                            cmd_string = command_template.substitute(
                                cmd=" ".join(cmd),
                                jobname=config_jobname,
                                mpi_ranks=mpi_ranks,
                                threads_per_rank=threads_per_rank,
                                ranks_per_node=ranks_per_node,
                                timeout=job_time_limit * 60,
                            )
                            commands.append(cmd_string)
                subs["commands"] = "\n".join(commands)
                subs["time_string"] = time.strftime(
                    format_duration(seconds=time_limit * 60)
                )
                job_script = template.substitute(subs)
                job_file = self.job_output_directory / aggregate_jobname
                with open(job_file, "w+") as job:
                    job.write(job_script)
                njobs += 1
        print(f"Created {njobs} job files in directory {self.job_output_directory}.")

    def required_nodes(self, cores, tasks_per_node):
        return int(max(int(m.ceil(float(cores) / tasks_per_node)), 1))

    def get_queue(self, cores, tasks_per_node, use_test_partition):
        raise NotImplementedError("Please implement this method.")

    def required_islands(self, nodes):
        raise NotImplementedError("Please implement this method.")


class SuperMUCRunner(SBatchRunner):

    def default_command_template(self):
        return command_template_dir / "command_template_intel.txt"

    def default_sbatch_template(self):
        return sbatch_template_dir / "supermuc.txt"

    def __init__(
        self,
        suite_name,
        experiment_data_directory,
        machine,
        output_directory,
        job_output_directory,
        sbatch_template,
        command_template,
        module_config,
        module_restore_cmd,
        tasks_per_node,
        time_limit,
        use_test_partition=False,
        omit_json_output_path=False,
        omit_seed=False,
    ):
        SBatchRunner.__init__(
            self,
            suite_name,
            experiment_data_directory,
            machine,
            output_directory,
            job_output_directory,
            sbatch_template,
            command_template,
            module_config,
            module_restore_cmd,
            time_limit,
            use_test_partition,
            omit_json_output_path,
            omit_seed
        )
        self.tasks_per_node = tasks_per_node if tasks_per_node is not None else 48

    def get_queue(self, cores, tasks_per_node, use_test_partition):
        nodes = self.required_nodes(cores, tasks_per_node)
        if nodes <= 16:
            return "test" if use_test_partition else "micro"
        elif nodes <= 768:
            return "general"
        else:
            return "large"

    def required_islands(self, nodes):
        if nodes > 768:
            return 2
        else:
            return 1


class HorekaRunner(SBatchRunner):

    def default_sbatch_template(self):
        return sbatch_template_dir / "horeka.txt"

    def __init__(
        self,
        suite_name,
        experiment_data_directory,
        machine,
        output_directory,
        job_output_directory,
        sbatch_template,
        command_template,
        module_config,
        module_restore_cmd,
        tasks_per_node,
        time_limit,
        use_test_partition=False,
        omit_json_output_path=False,
        omit_seed=False
    ):
        SBatchRunner.__init__(
            self,
            suite_name,
            experiment_data_directory,
            machine,
            output_directory,
            job_output_directory,
            sbatch_template,
            command_template,
            module_config,
            module_restore_cmd,
            time_limit,
            use_test_partition,
            omit_json_output_path,
            omit_seed,
        )
        self.tasks_per_node = tasks_per_node if tasks_per_node is not None else 76

    def get_queue(self, cores, tasks_per_node, use_test_partition):
        nodes = self.required_nodes(cores, tasks_per_node)
        if nodes <= 12:
            return "dev_cpuonly" if use_test_partition else "cpuonly"
        elif nodes <= 192:
            return "cpuonly"
        else:
            return ValueError("Cannot use more than 192 compute nodes on HoreKa!")

    def required_islands(self, nodes):
        return 1


class GenericDistributedMemoryRunner(SBatchRunner):
    def default_command_template(self):
        return command_template_dir / "command_template_generic.txt"

    def default_sbatch_template(self):
        return sbatch_template_dir / "generic_job_files.txt"

    def __init__(
        self,
        suite_name,
        experiment_data_directory,
        machine,
        output_directory,
        job_output_directory,
        sbatch_template,
        command_template,
        module_config,
        module_restore_cmd,
        tasks_per_node,
        time_limit,
        use_test_partition=False,
        omit_json_output_path=False,
        omit_seed=False,
    ):
        SBatchRunner.__init__(
            self,
            suite_name,
            experiment_data_directory,
            machine,
            output_directory,
            job_output_directory,
            sbatch_template,
            command_template,
            module_config,
            module_restore_cmd,
            time_limit,
            use_test_partition,
            omit_json_output_path,
            omit_seed,
        )
        self.tasks_per_node = tasks_per_node if tasks_per_node is not None else 1

    def get_queue(self, cores, tasks_per_node, use_test_partition):
        return "generic_partition"

    def required_islands(self, nodes):
        return 1


def get_runner(args, suite):
    # print("type: ", suite.suite_type)
    if args.machine == "shared":
        runner = SharedMemoryRunner(
            suite.name,
            args.max_cores,
            args.experiment_data_dir,
            args.output_dir,
            args.command_template,
            args.omit_json_output_path,
            args.omit_seed,
        )
        return runner

    elif args.machine in "supermuc":
        return SuperMUCRunner(
            suite.name,
            args.experiment_data_dir,
            args.machine,
            args.output_dir,
            args.job_output_dir,
            args.sbatch_template,
            args.command_template,
            args.module_config,
            args.module_restore_cmd,
            args.tasks_per_node,
            args.time_limit,
            args.test,
            args.omit_json_output_path,
            args.omit_seed,
        )
    elif args.machine in "horeka":
        return HorekaRunner(
            suite.name,
            args.experiment_data_dir,
            args.machine,
            args.output_dir,
            args.job_output_dir,
            args.sbatch_template,
            args.command_template,
            args.module_config,
            args.module_restore_cmd,
            args.tasks_per_node,
            args.time_limit,
            args.test,
            args.omit_json_output_path,
            args.omit_seed,
        )
    elif args.machine == "generic-job-file":
        return GenericDistributedMemoryRunner(
            suite.name,
            args.experiment_data_dir,
            args.machine,
            args.output_dir,
            args.job_output_dir,
            args.sbatch_template,
            args.command_template,
            args.module_config,
            args.module_restore_cmd,
            args.tasks_per_node,
            args.time_limit,
            args.test,
            args.omit_json_output_path,
            args.omit_seed,
        )
    else:
        exit("Unknown machine type: " + args.machine)
