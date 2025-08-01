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

from genericpath import isfile
import subprocess
import logging
import os, re
from pathlib import Path
import yaml
import sys
import math
import slugify
import copy


class InputGraph:
    def __init__(self, name):
        self._name = name

    def args(self, mpi_rank, threads_per_rank, escape):
        raise NotImplementedError()

    @property
    def name(self):
        return self._name


class FileInputGraph(InputGraph):
    def __init__(self, name, path, format="metis"):
        self._name = slugify.slugify(name)
        self.path = path
        self.format = format
        self.partitions = {}
        self.partitioned = False

    def args(self, mpi_ranks, threads_per_rank, escape):
        # file_args = [str(self.path), "--input-format", self.format]
        file_args = ["--graphtype", "BRAIN", "--infile_dir", str(self.path)]
        if self.partitioned and mpi_ranks > 1:
            partition_file = self.partitions.get(mpi_ranks, None)
            if not partition_file:
                logging.error(
                    f"Could not load partitioning for p={mpi_ranks} for input {self.name}"
                )
                sys.exit(1)
            file_args += ["--partitioning", partition_file]
        return file_args

    def add_partitions(self, partitions):
        self.partitions.update(partitions)

    @property
    def name(self):
        if self.partitioned:
            return self._name + "_partitioned"
        else:
            return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def exists(self):
        if self.format == "metis":
            return self.path.exists()
        elif self.format == "binary":
            root = self.path.parent
            first_out = root / (self.path.stem + ".first_out")
            head = root / (self.path.stem + ".head")
            return first_out.exists() and head.exists()
        elif self.format == "brain_format":
            return True

    def __repr__(self):
        return f"FileInputGraph({self.name, self.path, self.format, self.partitioned, self.partitions})"


class KaGenGraph(InputGraph):

    def __init__(self, **kwargs):
        kwargs = kwargs.copy()
        if not "type" in kwargs:
            raise ValueError("KaGen graph requires a type")
        try:
            self.n = kwargs.get("n", 1 << int(kwargs["N"]))
        except TypeError:
            self.n = None
        try:
            self.m = kwargs.get("m", 1 << int(kwargs["M"]))
        except TypeError:
            self.m = None
        kwargs.pop("n", None)
        kwargs.pop("N", None)
        kwargs.pop("m", None)
        kwargs.pop("M", None)
        self.scale_weak = kwargs.get("scale_weak", False)
        kwargs.pop("scale_weak", False)
        self.params = kwargs

    def get_n(self, p):
        if self.scale_weak:
            return self.n * p
        else:
            return self.n

    def get_m(self, p):
        if self.scale_weak:
            return self.m * p
        else:
            return self.m

    def args(self, mpi_ranks, threads_per_rank, escape):
        p = mpi_ranks * threads_per_rank
        params = self.stringify_params()
        if self.n:
            params.append(f"n={self.get_n(p)}")
        if self.m:
            params.append(f"m={self.get_m(p)}")
        kagen_option_string = ";".join(params)
        if escape:
            kagen_option_string = '"{}"'.format(kagen_option_string)
        return ["--kagen_option_string", kagen_option_string]

    def stringify_params(self):
        param_strings = []
        for key, value in self.params.items():
            if isinstance(value, bool):
                param_strings.append(key)
            else:
                param_strings.append(f"{key}={value}")
        return param_strings

    @property
    def name(self):
        params = []
        if self.n:
            params.append(f"n={int(math.log2(self.n))}")
        if self.m:
            params.append(f"m={int(math.log2(self.m))}")
        params += self.stringify_params()
        if self.scale_weak:
            params.append("weak")
        name = f"KaGen_{'_'.join(params)}"
        return slugify.slugify(name)


class DummyInstance(InputGraph):
    def __init__(self, **kwargs):
        self.name_ = kwargs["name"]
        self.params = kwargs.copy()
        self.params.pop("name", None)

    def args(self, mpi_rank, treeads_per_rank, escape):
        params = []
        for key, value in self.params.items():
            if key != "nokey":
                params.append(f"--{key}")
            if not isinstance(value, bool):
                params.append(f"\"{value}\"")
        return params

    @property
    def name(self):
        param_strings = []
        for key, value in self.params.items():
            if isinstance(value, bool):
                param_strings.append(key)
            else:
                param_strings.append(f"{key}={value}")
        name = self.name_ + "_" + "_".join(param_strings)
        return slugify.slugify(name)


class ExperimentSuite:
    def __init__(
        self,
        name: str,
        executable: None,
        cores=[],
        threads_per_rank=[1],
        inputs=[],
        configs=[],
        tasks_per_node=None,
        time_limit=None,
        seeds = [0],
        input_time_limit={},
    ):
        self.name = name
        self.executable = executable
        self.cores = cores
        self.threads_per_rank = threads_per_rank
        self.inputs = inputs
        self.configs = configs
        self.tasks_per_node = tasks_per_node
        self.time_limit = time_limit
        self.seeds = seeds
        self.input_time_limit = input_time_limit

    def set_input_time_limit(self, input_name, time_limit):
        self.input_time_limit[input_name] = time_limit

    def get_input_time_limit(self, input_name):
        return self.input_time_limit.get(input_name, self.time_limit)

    def load_inputs(self, input_dict, partitions):
        inputs_new = []
        for graph in self.inputs:
            if isinstance(graph, str):
                graph = {"name": graph, "partitioned": False}
            elif isinstance(graph, tuple):
                graph_name, partitioned = graph
                graph = {"name": graph_name, "partitioned": partitioned}
            else:
                inputs_new.append(graph)
                continue
            input = copy.copy(input_dict.get(graph["name"]))
            if not input:
                logging.warn(f"Could not load input for {graph_name}")
                continue
            if graph["partitioned"]:
                input.add_partitions(partitions.get(graph["name"], {}))
                input.partitioned = graph["partitioned"]
            # print(input)
            inputs_new.append(input)

        self.inputs = inputs_new
        # print(self.inputs)

    def __repr__(self):
        return f"ExperimentSuite({self.name}, {self.cores}, {self.inputs}, {self.configs}, {self.time_limit}, {self.input_time_limit})"


def load_suite_from_yaml(path):
    with open(path, "r") as file:
        data = yaml.safe_load(file)
    configs = []
    if "config" not in data:
        configs = [dict()]
    elif type(data["config"]) == list:
        for config in data["config"]:
            configs = configs + explode(config)
    else:
        configs = explode(data["config"])
    inputs = []
    time_limits = {}
    for graph in data["graphs"]:
        if type(graph) == str:
            inputs.append(graph)
        else:
            if "generator" in graph:
                generator = graph.pop("generator")
                if generator == "kagen":
                    inputs.extend([KaGenGraph(**graph_variant) for graph_variant in explode(graph)])
                elif generator == "dummy":
                    inputs.append(DummyInstance(**graph))
                else:
                    raise ValueError(
                        f"'{generator}' is an unsupported argument for a graph generator. Use ['kagen', 'dummy'] instead."
                    )
            else:
                raise ValueError(f"No generator defined for graph: {graph}.")
            time_limit = graph.get("time_limit")
            if time_limit:
                time_limits[graph["name"]] = time_limit
    if "executable" in data:
        executable = data["executable"]
    else:
        executable = None
    return ExperimentSuite(
        data["name"],
        executable,
        data["ncores"],
        data.get("threads_per_rank", [1]),
        inputs,
        configs,
        tasks_per_node=data.get("tasks_per_node"),
        time_limit=data.get("time_limit"),
        seeds=data.get("seeds", [0]),
        input_time_limit=time_limits,
    )


def explode(config):
    configs = []
    for flag, value in config.items():
        if type(value) == list:
            for arg in value:
                exploded = config.copy()
                exploded[flag] = arg
                exp = explode(exploded)
                configs = configs + exp
            break
    if not configs:
        return [config]
    return configs


def params_to_flags(params):
    flags = []
    for flag, value in params.items():
        dash = "-"
        if len(flag) > 1:
            dash += "-"
        if isinstance(value, bool):
            if value:
                flags.append(dash + flag)
        else:
            flags.append(dash + flag)
            flags.append(str(value))
    return flags


def command(
    binary_name, binary_path, input, mpi_ranks, threads_per_rank, escape, **kwargs
):
    script_path = os.path.dirname(__file__)
    build_dir = Path(
        os.environ.get("BUILD_DIR", os.path.join(script_path, "../build/"))
    )
    app = build_dir / binary_path / binary_name
    command = [str(app)]
    if input:
        if isinstance(input, InputGraph):
            command = command + input.args(mpi_ranks, threads_per_rank, escape)
        else:
            command.append(str(input))
    flags = []
    flags = flags + params_to_flags(kwargs)
    command = command + flags
    return command
