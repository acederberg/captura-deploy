"""Docker image build and deploy.

Unfortunately the awful builtkit issues still persist in the docker python 
library. 

I'd like to move on for now and come back here later.

This exists because I do not want to write a build and deploy step in multiple 
pipelines (since I will likely need a front end of some sort which will exist 
in a separate repository or specify another docker image.

I prefer to have much of the action happening on my own infrastructure as it 
offers the greatest visibility into what is happening within kubernetes. In the 
case of failures logs can be convienently view in the terminal and I'll still 
have access to the container that ran the job, which will be a much better 
experience than debugging tests that are directly happening in an action.

I would like to implement CI by deploying this project itself as an image (which
will also use this script to build, though the first of which will be published
outside of kubernetes). This image would be used for build jobs and test 
invokation.

An api request could be used to dispatch a job in kubernetes. Since this is 
likely long running, so websockets should probably be used to communicate the 
status of the job when prompted.

Process:

0. Write a dockerfile for the CI container.
1. Figure out how tests will be run in kubernetes. Python should somehow start
   a job in kubernetes using an image defined by the repo that is being built.
2. Build this software and develop a means to deploy and test itself it in k8s.
3. Develop a solution for captura.
4. MySql and captura deployment.


About Testsing
-------------------------------------------------------------------------------

With the builder-info configutation at hand, it will be easy to build and test 
a docker image. 
"""

# =========================================================================== #
import re
import selectors
import subprocess
import sys
from datetime import datetime
from typing import Annotated, Any, Callable, Dict, Generator, Literal, Self, Set

import docker
import git
import httpx
import typer
import yaml
from docker.models.images import Image
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from rich.console import Console
from rich.syntax import Syntax

# --------------------------------------------------------------------------- #
from captura_pipelines import flags
from captura_pipelines.config import PipelineConfig
from captura_pulumi import util
from captura_pulumi.porkbun import CONSOLE
from captura_pulumi.util import BaseYAML

BUILDFILE = "builder.yaml"
PATTERN_GITHUB = re.compile(
    "(?P<scheme>https|ssh)://(?P<auth>(?P<auth_username>[a-zA-Z0-9]+):?(?P<auth_password>.+)?@)?github.com/(?P<slug>(?P<username>[a-zA-Z0-9_-]+)/(?P<repository>[a-zA-Z0-9_-]+))(?P<dotgit>\\.git)?(?P<path>/.*)?"
)
LABEL_FROM = "builder"
PATH_CLONE = util.path.base(".builder")

logger = util.get_logger(__name__)


class BuilderImage(BaseYAML):
    repository: Annotated[
        str,
        Field(),
    ]
    tags: Annotated[
        Set[str],
        Field(default_factory=set),
    ]
    labels: Annotated[Dict[str, str], Field(default_factory=dict)]
    push: Annotated[bool, Field(default=False)]


class BuilderGit(BaseYAML):
    # Must be provided.
    repository: Annotated[str, Field()]
    path: Annotated[str, Field()]
    pull: Annotated[bool, Field(default=True)]

    branch: Annotated[str, Field()]
    tag: Annotated[
        str | None,
        Field(default=None, description="Tag to checkout and build."),
    ]
    commit: Annotated[
        str | None,
        Field(
            default=None,
            description="Hash to checkout and build. Populated when ``configure`` is called.",
        ),
    ]

    # Optional.
    # dockerdir: Annotated[
    #     str | None,
    #     Field(
    #         description="Relative path to docker in the git repositoy",
    #         default="docker",
    #     ),
    # ]
    dockerfile: Annotated[
        str | None,
        Field(
            description="Path of dockerfile relative to project root.",
            default="dockerfile",
        ),
    ]
    dockertarget: Annotated[
        str | None,
        Field(
            description="Builder target. For multistaged builds",
            default=None,
        ),
    ]

    @model_validator(mode="before")
    def check_repository(cls, values):

        if (repository := values.get("repository")) is None or "path" in values:
            return values

        matched = PATTERN_GITHUB.match(repository)
        if matched is None:
            msg = f"`{repository}` must match pattern `{PATTERN_GITHUB}`."
            raise ValueError(msg)

        values["path"] = util.p.join(PATH_CLONE, matched.group("repository"))
        return values

    @classmethod
    def ensure(cls, repository: str, path: str) -> git.Repo:
        repo = (
            git.Repo.clone_from(repository, to_path=path)
            if path is None or not util.p.exists(path)
            else git.Repo(path)
        )
        return repo

    def configure(self):
        path = self.path
        if util.p.exists(path) and not util.p.isdir(path):
            raise ValueError(f"Clone path `{path}` must be a directory.")

        repo = self.ensure(self.repository, path)

        branch: git.Head | None
        if (branch := getattr(repo.heads, self.branch, None)) is None:
            msg = f"No such branch `{self.branch}` of `{self.repository}`."
            raise ValueError(msg)

        if self.pull:
            repo.remotes["origin"].pull()

        if self.commit is not None:
            branch.set_commit(self.commit)
        else:
            self.commit = branch.object.hexsha

        return


class BuilderOptions(BaseYAML):
    tier: Annotated[util.LabelTier, Field()]


class Builder(BaseYAML):

    config: PipelineConfig  # Mixed in, not defined in YAML unless overwrite.
    git: BuilderGit
    image: BuilderImage
    options: BuilderOptions
    origin: Annotated[None | str, Field(default=None)]

    @classmethod
    def fromBuilderFile(
        cls,
        config: PipelineConfig,
        *,
        url: str | None = None,
        path: str | None = None,
        overwrite: Dict[str, Any] | None = None,
    ) -> Self:
        """Create an instance given a path or url to yaml specifying an
        instance.
        """

        logger.debug("Creating builder instance from build file.")
        if (url is not None and path is not None) or (url is None and path is None):
            raise ValueError("Exactly one of `url` and `path` should be specified.")

        exclude = dict(config=config)

        # NOTE: Look for build info instead of cloning and expecting it.
        if url is not None:
            res = httpx.get(url)
            if res.status_code != 200:
                msg = f"Could not find build info at `{url}`."
                raise ValueError(msg)

            content = yaml.safe_load(res.content)
            return cls.fromYAML(loaded=[content], overwrite=overwrite, exclude=exclude)
        else:
            assert path is not None
            return cls.fromYAML(path, overwrite=overwrite, exclude=exclude)

    @classmethod
    def fromRepo(
        cls,
        config: PipelineConfig,
        repository_url: str,
        branch: str = "master",
        overwrite: Dict[str, Any] | None = None,
    ) -> Self:
        """This function assumes that images are published via github."""

        logger.debug("Getting `%s` -> `%s` -> `builder.yaml`.", repository_url, branch)
        matched = PATTERN_GITHUB.match(repository_url)
        if matched is None:
            raise ValueError(f"[red]Could not match `{repository_url}`.")

        slug = matched.group("slug")
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/{BUILDFILE}"

        builder = Builder.fromBuilderFile(config, url=url, overwrite=overwrite)
        return builder  # type: ignore

    @classmethod
    def forTyper(
        cls,
        context: typer.Context,
        git_repository_url: flags.FlagGitRepository,
        git_branch: flags.FlagBranch = "master",
        git_tag: flags.FlagGitTag = None,
        git_commit: flags.FlagGitCommit = None,
    ) -> Self:

        overwrite = dict(tag=git_tag, commit=git_commit)
        overwrite = {k: v for k, v in overwrite.items() if v is not None}
        try:
            builder = cls.fromRepo(
                context.obj.config,
                git_repository_url,
                git_branch,
                overwrite=dict(git=overwrite),
            )
        except ValueError as err:
            CONSOLE.print("[reGid]" + str(err))
            raise typer.Exit(1)

        return builder

    @computed_field
    @property
    def image_full(self) -> str:
        # NOTE: For docker hub. Namespace is always the user name. Otherwise,
        #       no namespace is added - instead the repository is added.
        ns_or_registry = (
            self.config.registry.username
            if self.config.registry.registry is None
            else self.config.registry.registry
        )

        return f"{ns_or_registry}/{self.image.repository}"

    @computed_field
    @property
    def image_tags(self) -> Set[str]:
        """Always tagged by ``git_hash``.

        The following branches have special (docker) tags when (git) tags are
        pushed:

        .. code:: txt

           master/main -> {Pyproject TOML version}-alpha
           dev         -> {pyproject TOML version}-beta
           release     -> {Pyproject TOML version}

        """

        tags = set()
        print(tags)
        if self.git.commit is not None:
            tags.add(self.git.commit)

        print(tags)
        if self.git.tag:
            version = self.git.tag
            if self.git.branch == "master" or self.git.branch == "main":
                version = f"{self.git.tag}-alpha"
            elif self.git.branch == "dev":
                version = f"{self.git.tag}-beta"

            tags.add(version)

        tags |= self.image.tags
        print(tags)
        return {f"{self.image_full}:{tag}" for tag in tags}

    @computed_field
    @property
    def image_labels(self) -> Dict[str, str]:
        # registry = self.image.registry.registry
        return util.create_labels(
            tier=self.options.tier,
            component=util.LabelComponent.registry,
            from_=LABEL_FROM,
            **self.image.labels,
        )

    def req_list_tags(self) -> httpx.Request:
        return httpx.Request(
            "GET",
            self.config.registry.registry_url(self.image.repository, "tags", "list"),
            headers=self.config.registry.headers(),
        )

    def execute(self, client: docker.DockerClient) -> Generator[Any, None, None]:
        logger.debug("Building `%s`.", self.image_tags)
        config = self.config
        client = client if client is not None else config.registry.create_client()
        yield from self.build(client)
        self.push(client)

    def build(
        self,
        client: docker.DockerClient,
        callback: Callable[[Any, Any], Any],
        handle_exit: Callable[[int], Exception] | None = None,
    ) -> Generator[Any, None, None]:
        self.git.configure()

        tags = self.image_tags.copy()
        tag = tags.pop()

        # ------------------------------------------------------------------- #
        # NOTE: Because library does not support modern builds.

        cmd = ["docker", "build"]
        if self.git.dockertarget is not None:
            cmd += ("--target", self.git.dockertarget)
        if self.git.dockerfile is not None:
            cmd += ("--file", util.p.join(self.git.path, self.git.dockerfile))

        cmd += ("--tag", tag)
        cmd.append(self.git.path)

        logger.debug("Building with command `%s`.", cmd)
        with subprocess.Popen(cmd, stdout=subprocess.PIPE) as process:
            assert process.stdout is not None

            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, callback)

            while process.poll() is None:
                events = selector.select()
                for key, mask in events:
                    fn = key.data
                    yield fn(key.fileobj, mask)

            exit_code = process.wait()

        if exit_code:
            err = (
                ValueError(f"Build exitted with code `{exit_code}`.")
                if handle_exit is None
                else handle_exit(exit_code)
            )
            raise err

        # ------------------------------------------------------------------- #

        image = client.images.get(tag)
        for tag in tags:
            logger.debug("Tagging with `%s`.", tag)
            image.tag(tag)

    def push(self, client: docker.DockerClient) -> None:
        if not self.image.push:
            return

        logger.info("Pushing `%s`.", self.image_tags)
        for tag in self.image_tags:
            logger.debug("Pushing tag `%s`.", tag)
            image_full, tag = tag.split(":")
            client.images.push(image_full, tag=tag)


class BuilderCommandCI:
    @classmethod
    def push(
        cls,
        context: typer.Context,
        repository_url: flags.FlagGitRepository,
        branch: flags.FlagBranch = "master",
        git_tag: flags.FlagGitTag = None,
        git_commit: flags.FlagGitCommit = None,
    ):
        builder = Builder.forTyper(
            context,
            repository_url,
            branch,
            git_tag=git_tag,
            git_commit=git_commit,
        )
        builder.git.configure()
        builder.image.push = True

        builder.push(builder.config.registry.create_client())

        # rendered = yaml.dump(builder.model_dump(mode="json"))
        # rendered = f"---\n# Rendered from `{builder.origin}`\n" + rendered
        #
        # util.print_yaml(rendered, is_dumped=True)

    @classmethod
    def build(
        cls,
        context: typer.Context,
        repository_url: flags.FlagGitRepository,
        branch: flags.FlagBranch = "master",
        git_tag: flags.FlagGitTag = None,
        git_commit: flags.FlagGitCommit = None,
    ):
        builder = Builder.forTyper(
            context,
            repository_url,
            branch,
            git_tag=git_tag,
            git_commit=git_commit,
        )

        # NOTE: See https://stackoverflow.com/questions/18421757/live-output-from-subprocess-command
        ts = datetime.now().isoformat(sep="-")
        logfile_path = util.path.logs(f"docker-build-{builder.origin}-{ts}.log")

        def handle_exit(exit_code: int):
            CONSOLE.print(f"[red]Build failed with exit code `{exit_code}`.")
            return typer.Exit(exit_code)

        builder.git.configure()
        with open(logfile_path, "w") as logfile:
            for _ in builder.build(
                builder.config.registry.create_client(),
                lambda stream, _: tuple(
                    buffer.write(chunk)
                    for buffer in (sys.stdout.buffer, logfile.buffer)
                    for chunk in (stream.readline(),)
                    if chunk is not None
                ),
                handle_exit=handle_exit,
            ):
                ...

    @classmethod
    def initialize(
        cls,
        context: typer.Context,
        repository_url: flags.FlagGitRepository,
        branch: flags.FlagBranch = "master",
    ):
        builder = Builder.forTyper(context, repository_url, branch)
        builder.git.configure()

    @classmethod
    def list(
        cls,
        context: typer.Context,
        repository_url: flags.FlagGitRepository,
        branch: flags.FlagBranch = "master",
    ):

        builder = Builder.forTyper(context, repository_url, branch)
        with httpx.Client() as client:
            req = client.send(builder.req_list_tags())
            data, err = util.check(req)
            if err is not None:
                raise err

        util.print_yaml(data)

    @classmethod
    def hydrate(
        cls,
        context: typer.Context,
        repository_url: flags.FlagGitRepository,
        branch: flags.FlagBranch = "master",
        git_tag: flags.FlagGitTag = None,
        git_commit: flags.FlagGitCommit = None,
    ):

        builder = Builder.forTyper(
            context,
            repository_url,
            branch,
            git_tag,
            git_commit,
        )
        builder.git.configure()
        rendered = yaml.dump(builder.model_dump(mode="json"))
        rendered = f"---\n# Rendered from `{builder.origin}`\n" + rendered

        util.print_yaml(rendered, is_dumped=True)

    @classmethod
    def create_typer(cls):
        cli = typer.Typer()
        cli.command("push")(cls.push)
        cli.command("hydrate")(cls.hydrate)
        cli.command("build")(cls.build)
        cli.command("ci")(cls.push)
        cli.command("ls")(cls.list)
        cli.command("initialize")(cls.initialize)

        return cli


# NOTE: Supports multiple files since it will be convenient to keep partial
#       build info YAML in repositories. When the repo is cloned, it should
#       specify ``build-info.yaml`` and ``build-info.test.yaml``.
class BuilderCommand:

    @classmethod
    def list_catalog(cls, context: typer.Context):
        context_data: flags.ContextData = context.obj

        with httpx.Client() as client:
            req = client.send(context_data.config.registry.req_catalog())
            data, err = util.check(req)
            if err is not None:
                raise err

        util.print_yaml(data)

    @classmethod
    def create_typer(cls):
        cli = typer.Typer()
        cli.command("list")(cls.list_catalog)
        cli.add_typer(BuilderCommandCI.create_typer(), name="ci")

        return cli
