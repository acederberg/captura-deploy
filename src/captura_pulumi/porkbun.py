"""Module for updating porkbun dns after swapping out node. To read about the 
porkbun api see https://porkbun.com/api/json/v3/documentation.
"""

# =========================================================================== #
import asyncio
import functools
import json
import re
from datetime import datetime
from typing import (
    Annotated,
    Any,
    AsyncGenerator,
    Callable,
    ClassVar,
    Dict,
    Generator,
    Literal,
    ParamSpec,
    Self,
    Set,
    Tuple,
)

import httpx
import pulumi
from pydantic import Field
from rich.console import Console
from yaml_settings_pydantic import BaseYamlSettings, YamlSettingsConfigDict

# --------------------------------------------------------------------------- #
from captura_pulumi import util

P_Porkbun = ParamSpec("P_Porkbun")
RecordType = Literal["A", "DNS", "CNAME"]

CONSOLE = Console()


# NOTE: API reference: https://porkbun.com/api/json/v3/documentation
class PorkbunRequests(BaseYamlSettings):
    api_url: Annotated[str, Field(default="https://porkbun.com/api/json/v3")]
    api_key: str
    secret_key: str

    model_config: ClassVar[YamlSettingsConfigDict] = YamlSettingsConfigDict(
        yaml_files=util.path.config("porkbun.yaml")
    )

    # ----------------------------------------------------------------------- #

    @classmethod
    def from_config(
        cls,
        config: pulumi.Config,
    ) -> pulumi.Output[Self]:

        return pulumi.Output.all(
            config.require_secret("porkbun_secret_key"),
            config.require_secret("porkbun_api_key"),
        ).apply(
            lambda data: cls(  # type: ignore
                secret_key=data[0],
                api_key=data[1],
            ),
        )

    @property
    def authfields(self) -> Dict[str, Any]:
        return dict(secretapikey=self.secret_key, apikey=self.api_key)

    @functools.cached_property
    def headers(self):
        return {"Content-Type": "Application/JSON"}

    def url(self, *parts: str) -> str:
        return "/".join((self.api_url, *parts))

    # ----------------------------------------------------------------------- #
    # Requests

    def req_ping(self) -> httpx.Request:
        "Verify that the token works."

        data = self.authfields
        url = self.url("ping")
        return httpx.Request("POST", url, json=data, headers=self.headers)

    def req_read_domain(
        self,
        domain: str,
        *,
        record_type: RecordType | None = None,
        subdomain: str | None = None,
    ) -> httpx.Request:

        data = self.authfields
        if record_type is not None:
            url = self.url("dns", "retrieveByNameType", domain, record_type)
            if subdomain:
                url += "/" + subdomain
        else:
            url = self.url("dns", "retrieve", domain)

        return httpx.Request("POST", url, json=data, headers=self.headers)

    # Fails spontainiously with `{'status': 'ERROR', 'message': 'Edit error: We were unable to edit the DNS record.'}`.
    def req_update_domain_record(
        self,
        domain: str,
        id: str,
        *,
        content: str,
        record_type: RecordType,
        name: str | None = None,
        ttl: int | None = None,
    ) -> httpx.Request:
        data = self.authfields
        data.update(content=content, type=record_type)
        if name is not None:
            data.update(name=name)
        if ttl is not None:
            data.update(ttl=ttl)

        url = self.url("dns", "edit", domain, id)
        return httpx.Request("POST", url, json=data, headers=self.headers)

    def req_delete_domain_record(
        self,
        domain: str,
        id: str,
    ) -> httpx.Request:
        data = self.authfields
        url = self.url("dns", "delete", domain, id)
        return httpx.Request("POST", url, json=data, headers=self.headers)

    def req_create_domain_record(
        self,
        domain: str,
        *,
        name: str | None,
        record_type: RecordType,
        content: str,
    ):
        data = self.authfields
        data.update(type=record_type, content=content)
        if name is not None:
            data.update(name=name)

        url = self.url("dns", "create", domain)
        return httpx.Request("POST", url, json=data, headers=self.headers)

    # ----------------------------------------------------------------------- #
    # Executers

    def check(
        self, res: httpx.Response, *, status_code: int = 200
    ) -> Tuple[Any, AssertionError | None]:
        data = res.json()
        if res.status_code != status_code:
            msg = "Expected response status code `{}`, got `{}`. Data=`{}`."
            err = AssertionError(msg.format(res.status_code, status_code, data))
            return data, err

        err = None
        if data["status"] != "SUCCESS":
            err = AssertionError(
                f"`status` should be `SUCCESS`, got `{data['success']}`."
            )

        return data, err

    async def dispatch(
        self,
        client: httpx.AsyncClient,
        fn: Callable[P_Porkbun, Any],
        *args: P_Porkbun.args,
        **kwargs: P_Porkbun.kwargs,
    ) -> Any:
        """Use this to send one off requests. This opens a new client every
        time it is called."""

        res = await client.send(fn(*args, **kwargs))
        data = self.check(res, status_code=200)
        console.print_json(json.dumps(data))

    async def replace(
        self,
        client: httpx.AsyncClient,
        domain: str,
        ipaddr: str,
        *,
        subdomain: str,
        # force: bool = False,
    ) -> AsyncGenerator[str, None]:

        p = re.compile(
            f"(?P<subdomain_all>(?P<subdomain_name>[*a-zA-Z0-9_-]*)\\.?){domain}"
        )
        m = p.match(subdomain)
        if m is None:
            raise ValueError(f"Failed to match subdomain name of `{subdomain}`.")
        subdomain_name = m.group("subdomain_name")

        # NOTE: Read existing and delete if applicable.
        req_read = self.req_read_domain(
            domain, record_type="A", subdomain=subdomain_name
        )
        res_read = await client.send(req_read)
        data, err = self.check(res_read)
        if err is not None:
            raise err

        msg_chain = f"{domain} -> {subdomain_name} -> {ipaddr}"
        assert (records := data.get("records")) is not None
        match records:
            case (record,):
                yield "Found record " + msg_chain
                if record["content"] == ipaddr:
                    return

                req_delete = self.req_delete_domain_record(domain, record["id"])
                await client.send(req_delete)
            case ():
                yield "No record found for " + msg_chain
            case bad:
                raise ValueError(f"Refusing to handle `{bad}`.")

        # NOTE: Read and verify nothing
        yield "Verifying that records cleared for " + msg_chain
        res_read = await client.send(req_read)
        data, err = self.check(res_read)
        if err is not None:
            raise err

        assert (records := data.get("records")) is not None
        assert isinstance(records, list) and len(records) == 0

        # NOTE: Create.
        yield "Creating records for " + msg_chain

        req_create = self.req_create_domain_record(
            domain, name=subdomain_name, record_type="A", content=ipaddr
        )
        res_create = await client.send(req_create)
        data, err = self.check(res_create)
        if err is not None:
            raise err

        yield "Record created!"

    async def __call__(
        self,
        client: httpx.AsyncClient,
        domain: str,
        ipaddr: str,
        *,
        subdomains: Set[str],
    ):
        """Adding ``subdomains`` will include the root record."""

        res_ping = await client.send(self.req_ping())
        _, err = self.check(res_ping)
        if err is not None:
            raise err

        for subdomain in subdomains:
            async for line in self.replace(
                client, domain, ipaddr=ipaddr, subdomain=subdomain
            ):
                yield line


# ---------------------------------------------------------------------------


async def handle_porkbun(*, domain: str, ipaddr: str):
    """Use apply to point the domain name at the new node balancer."""

    porkbun = PorkbunRequests()  # type: ignore
    subdomains = {domain, f"*.{domain}", f"www.{domain}"}
    now = datetime.now()

    util.ensure(util.PATH_LOGS)

    async with httpx.AsyncClient() as client:
        with open(util.path.logs(f"porkbun-{now.timestamp()}.log"), "a") as file:
            file.write(80 * "=")
            file.write("\nLogs for `handle_porkbun`\n\n")
            file.write("Timestamp: " + str(now))
            async for line in porkbun(client, domain, ipaddr, subdomains=subdomains):
                file.write(line + "\n")
            file.write("\n")
