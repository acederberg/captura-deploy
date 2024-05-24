"""Docker image build and deploy.

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
from typing import Annotated, Dict, Literal, Self, Set

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
from captura_pulumi.util import BaseYAML

BUILDFILE = "build.yaml"
PATTERN_GITHUB = re.compile(
    "(?P<scheme>https|ssh)://(?P<auth>(?P<auth_username>[a-zA-Z0-9]+):?(?P<auth_password>.+)?@)?github.com/(?P<slug>(?P<username>[a-zA-Z0-9_-]+)/(?P<repository>[a-zA-Z0-9_-]+))(?P<dotgit>\\.git)?(?P<path>/.*)?"
)
LABEL_FROM = "builder"
PATH_CLONE = util.path.base(".builder")


class BuilderImage(BaseYAML):
    repository: Annotated[
        str,
        Field(),
    ]
    tags: Annotated[
        Set[str],
        Field(default_factory=list),
    ]
    labels: Annotated[Dict[str, str], Field(default_factory=dict)]
    push: Annotated[bool, Field(default=False)]


class BuilderGit(BaseYAML):
    # Must be provided.
    repository: Annotated[str, Field()]
    path: Annotated[str, Field()]
    pull: Annotated[bool, Field(default=False)]

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

    # @computed_field
    # @property
    # def git_path(self) -> str:
    #     return util.p.join(PATH_CLONE, self.image.repository)

    @classmethod
    def ensure(cls, repository: str, path: str) -> git.Repo:
        print(util.p.abspath(path))
        repo = (
            git.Repo.clone_from(repository, to_path=path)
            if path is None or not util.p.exists(path)
            else git.Repo(path)
        )
        return repo

    def configure(self, path: str):
        if util.p.exists(path) and not util.p.isdir(path):
            raise ValueError(f"Clone path `{path}` must be a file.")

        repo = self.ensure(self.repository, path)

        branch: git.Head | None
        if (branch := getattr(repo.heads, self.branch, None)) is None:
            msg = f"No such branch `{self.branch}` of `{self.repository}`."
            raise ValueError(msg)

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

    @classmethod
    def fromBuilderFile(
        cls,
        config: PipelineConfig,
        build_yaml_url_raw: str,
    ) -> Self:

        # NOTE: Look for build info instead of cloning and expecting it.
        res = httpx.get(build_yaml_url_raw)
        if res.status_code != 200:
            msg = f"Could not find build info at `{build_yaml_url_raw}`."
            raise ValueError(msg)

        return cls.fromYAML(
            overwrite=yaml.safe_load(res.content),
            exclude={"config": config},
        )

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
        tags = {self.git.commit}
        if self.git.tag:
            version = self.git.tag
            if self.git.branch == "master" or self.git.branch == "main":
                version = f"{self.git.tag}-alpha"
            elif self.git.branch == "dev":
                version = f"{self.git.tag}-beta"

            tags.add(version)

        tags |= self.image.tags
        return {f"{util.DOMAIN}/{self.image.repository}:{tag}" for tag in tags}

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

    # @computed_field
    # @property
    # def git_path(self) -> str:
    #     return util.p.join(PATH_CLONE, self.image.repository)

    def execute(self, config: PipelineConfig) -> None:
        self.git.configure(self.git.path)
        client = config.registry.create_client()
        path = self.git.path

        image, rest = client.images.build(
            path=path,
            dockerfile=self.git.dockerfile,
            target=self.git.dockertarget,
            pull=True,
        )
        print(rest)

        for tag in self.image_tags:
            image.tag(tag)
            if self.options.push:
                client.images.push(self.image.repository, tag=tag)


# NOTE: Supports multiple files since it will be convenient to keep partial
#       build info YAML in repositories. When the repo is cloned, it should
#       specify ``build-info.yaml`` and ``build-info.test.yaml``.
class BuilderCommand:

    @classmethod
    def ci(cls, context: typer.Context, repository_url: str):
        """This function assumes that images are published via github."""

        # GET FROM ARBITRARY BRANCH BECAUSE WE NEED ATLEAST THE CLONE INFO.
        # https://raw.githubusercontent.com/acederberg/captura/master/pyproject.toml

        context_data: flags.ContextData = context.obj
        matched = PATTERN_GITHUB.match(repository_url)
        if matched is None:
            console.print(f"[red]Could not match `{repository_url}`.")
            raise typer.Exit(1)

        slug = matched.group("slug")
        rawurl = f"https://raw.githubusercontent.com/{slug}/{BUILDFILE}"

        try:
            build_info = Builder.fromYAML(context_data.config, rawurl)
        except ValueError as err:
            console.print("[red]" + str(err))
            raise typer.Exit(2)

        build_info.execute(context_data.config)

    @classmethod
    def build(cls, context: typer.Context, files: flags.FlagFile):

        context_data: flags.ContextData = context.obj

        build_info = Builder.fromYAML(*files, config=context_data.config)
        build_info.execute(context_data.config)

    @classmethod
    def hydrate(
        cls,
        context: typer.Context,
        files: flags.FlagFile,
        raw: bool = False,
    ):

        context_data: flags.ContextData = context.obj

        build_info = Builder.fromYAML(*files, config=context_data.config)
        build_info.git.configure(build_info.git.path)

        exclude = set()
        if not all:
            exclude = {"config"}
        rendered = build_info.model_dump(mode="json", exclude=exclude)
        rendered = yaml.dump(rendered)

        rendered = f"---\n# Rendered from `{files}`\n\n" + rendered
        if not raw:
            console = Console()
            console.print(Syntax(rendered, "yaml", background_color="default"))
            return

    @classmethod
    def create_typer(cls):
        cli = typer.Typer()
        cli.command("build")(BuilderCommand.build)
        cli.command("hydrate")(BuilderCommand.hydrate)
        return cli
