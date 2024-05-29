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
from typing import Annotated, Any, Dict, Generator, Literal, Self, Set

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
            description="Name of the dockerfile in ``dockerdir``.",
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
    ) -> Self:

        logger.debug("Creating builder instance from build file.")
        if url is not None and path is not None:
            raise ValueError()
        elif url is None and path is None:
            raise ValueError()

        # NOTE: Look for build info instead of cloning and expecting it.
        if url is not None:
            res = httpx.get(url)
            if res.status_code != 200:
                msg = f"Could not find build info at `{url}`."
                raise ValueError(msg)
            return cls.fromYAML(
                overwrite=yaml.safe_load(res.content),
                exclude={"config": config},
            )
        else:
            assert path is not None
            return cls.fromYAML(path, exclude={"config": config})

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
        if self.git.commit is not None:
            tags.add(self.git.commit)

        if self.git.tag:
            version = self.git.tag
            if self.git.branch == "master" or self.git.branch == "main":
                version = f"{self.git.tag}-alpha"
            elif self.git.branch == "dev":
                version = f"{self.git.tag}-beta"

            tags.add(version)

        tags |= self.image.tags
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

    def build(self, client: docker.DockerClient) -> Generator[Any, None, None]:
        self.git.configure()
        path = self.git.path

        tags = self.image_tags.copy()
        tag = tags.pop()

        logger.debug("Building...")
        yield from client.api.build(
            path=path,
            dockerfile=self.git.dockerfile,
            target=self.git.dockertarget,
            tag=tag,
            pull=True,
            # decode=True,
        )

        image = client.images.get(tag)
        logger.debug("Tagging...")
        for tag in tags:
            image.tag(tag)

    def push(self, client: docker.DockerClient) -> None:
        logger.info("Pushing `%s`.", self.image_tags)
        if self.image.push:
            client.images.push(self.image.repository)

    @classmethod
    def fromRepo(
        cls, config: PipelineConfig, repository_url: str, branch: str = "master"
    ) -> Self:
        """This function assumes that images are published via github."""

        logger.debug("Getting `%s` -> `%s` -> `builder.yaml`.", repository_url, branch)
        matched = PATTERN_GITHUB.match(repository_url)
        if matched is None:
            raise ValueError(f"[red]Could not match `{repository_url}`.")

        slug = matched.group("slug")
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/{BUILDFILE}"

        builder = Builder.fromBuilderFile(config, url=url)
        return builder  # type: ignore

    @classmethod
    def forTyper(
        cls,
        context: typer.Context,
        repository_url: str,
        branch: str = "master",
    ) -> Self:
        try:
            builder = cls.fromRepo(context.obj.config, repository_url, branch)
        except ValueError as err:
            CONSOLE.print("[red]" + str(err))
            raise typer.Exit(1)

        return builder


class BuilderCommandCI:
    @classmethod
    def push(
        cls,
        context: typer.Context,
        repository_url: flags.FlagRepository,
        branch: flags.FlagBranch = "master",
    ):
        builder = Builder.forTyper(context, repository_url, branch)
        builder.push(builder.config.registry.create_client())

    @classmethod
    def build(
        cls,
        context: typer.Context,
        repository_url: flags.FlagRepository,
        branch: flags.FlagBranch = "master",
    ):
        builder = Builder.forTyper(context, repository_url, branch)
        for line in builder.build(builder.config.registry.create_client()):
            CONSOLE.print(line)

    @classmethod
    def initialize(
        cls,
        context: typer.Context,
        repository_url: flags.FlagRepository,
        branch: flags.FlagBranch = "master",
    ):
        builder = Builder.forTyper(context, repository_url, branch)
        builder.git.configure()

    @classmethod
    def list(
        cls,
        context: typer.Context,
        repository_url: flags.FlagRepository,
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
        repository_url: flags.FlagRepository,
        branch: flags.FlagBranch = "master",
    ):

        builder = Builder.forTyper(context, repository_url, branch)
        builder.git.configure()
        rendered = yaml.dump(builder.model_dump(mode="json"))
        rendered = f"---\n# Rendered from `{builder.origin}`\n" + rendered

        util.print_yaml(rendered, is_dumped=True)

    @classmethod
    def create_typer(cls):
        cli = typer.Typer()
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
