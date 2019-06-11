from pathlib import PurePath

from .util import import_file
from .variables import TerraformVariableStore, VariableValue, get_variables_from_block


class Block:
    def __init__(self, path, body=None):
        self.__path = path
        self.__body = body or {}

    def __iter__(self):
        result = {}
        if "." in self.__path:
            here = result
            for part in self.__path.split("."):
                here[part] = {}
                here = here[part]
            here.update(self.__body)
        else:
            result[self.__path] = self.__body
        for key, value in result.items():
            yield (key, value)

    def __getattr__(self, name):

        parts = self.__path.split(".")

        if parts[0] == "resource":
            parts.pop(0)
        elif parts[0] == "variable":
            parts[0] = "var"
        elif parts[0] == "provider":
            alias = self.__body.get("alias")
            if alias:
                return f"{parts[1]}.{alias}"
            else:
                return parts[1]

        parts.append(name)

        return Interpolated(".".join(parts))

    __getitem__ = __getattr__

    def __repr__(self):
        return f"tf({repr(self.__path)}, {repr(self.__body)})"

    def __str__(self):
        return self.__path


class Interpolated:
    def __init__(self, value):
        self.__value = value

    def __eq__(self, other):
        return str(self) == other

    def __getattr__(self, attr):
        return type(self)(self.__value + "." + attr)

    def __repr__(self):
        return f"Interpolated({repr(self.__value)})"

    def __str__(self):
        return "${" + self.__value + "}"


class Renderer:
    def __init__(self, files_to_create):
        # These are all of the files that will be created.
        self.files_to_create = files_to_create

        # Variables will be populated from environment variables,
        # command line arguments, and files, as per standard Terraform
        # behaviour. They will also be populated as files get created.
        self.variables = TerraformVariableStore(
            files_to_create=files_to_create, process_jobs=self.process_jobs
        )

        # These are all of the jobs to create files.
        self.jobs = []
        for file_path in self.files_to_create.values():
            job = RenderJob(path=file_path, variables=self.variables)
            self.jobs.append(job)

        # This will be populated with blocks from each file being created.
        self.done = []

    def process_jobs(self, until=None):
        while self.jobs:
            if until and until in self.variables:
                break
            job = self.jobs.pop()
            done = job.run()
            if done:
                self.done.append(job)
            else:
                self.jobs.append(job)

    def render(self):
        self.process_jobs()
        results = {}
        for job in self.done:
            results[job.output_path] = job.contents()
        return results


class RenderJob:
    def __init__(self, path, variables):

        self.path = path
        self.variables = variables

        # Create a var object to pass into the file's terraform() generator.
        # This allows attribute and dict access to the variables.
        var = variables.proxy(path)

        # Load the file and start the terraform() generator.
        with import_file(path) as module:
            self.gen = module.terraform(var)

        self.done = False
        self.output_path = path.with_suffix(".json")
        self.output_name = self.output_path.name
        self.return_value = None

        self.blocks = []

    def contents(self):
        if self.output_name.endswith(".tfvars.json"):
            merged = {}
            for block in self.blocks:
                for name, value in block.items():
                    merged[name] = value
            return merged
        else:
            return self.blocks

    def process_tf_block(self, block):
        for var in get_variables_from_block(block, self.path.name):
            # Add the variable definition. This doesn't necessarily
            # make it available to use, because a tfvars file may
            # populate it later.
            self.variables.add(var)

    def process_tfvars_block(self, block):
        # Only populate the variable store with values in this file
        # if it is waiting for this file. It is possible to generate
        # tfvars files that don't get used as a source for values.
        if self.variables.tfvars_waiting_for(self.output_name):
            for name, value in block.items():
                # Add the variable value. Raise an error if it changes
                # the value, because it could result in Pretf using
                # the old value and Terraform using the new one.
                var = VariableValue(name=name, value=value, source=self.path.name)
                self.variables.add(var, allow_change=False)

    def run(self):

        try:
            yielded = self.gen.send(self.return_value)
        except StopIteration:
            self.variables.file_created(self.output_name)
            return True

        self.return_value = yielded

        for block in unwrap_yielded(yielded):

            if self.output_name.endswith(".tfvars.json"):
                self.process_tfvars_block(block)
            else:
                self.process_tf_block(block)

            self.blocks.append(block)

        return False


def json_default(obj):
    if isinstance(obj, (Interpolated, PurePath)):
        return str(obj)
    raise TypeError(repr(obj))


def unwrap_yielded(yielded):
    if isinstance(yielded, Block):
        yield dict(iter(yielded))
    elif isinstance(yielded, dict):
        yield yielded
    else:
        yield from yielded
