"""Kubernetes for traefik, container registry, and error-pages.


Do not add kubernetes resources for applications here, this is the 'back-ends 
back-end'. All functions in this module require that a provider has been set up 
first.

Resource:

.. code:: text

   [1] https://github.com/traefik/traefik-helm-chart/blob/master/EXAMPLES.md#use-traefik-native-lets-encrypt-integration-without-cert-manager
   [2] https://www.pulumi.com/registry/packages/kubernetes/how-to-guides/choosing-the-right-helm-resource-for-your-use-case/
   [3] https://github.com/twuni/docker-registry.helm/blob/main/templates/secret.yaml
       - Configuration specifiying the secret for the twuni helm chart.
       - Should probably fork and improve, lots of issues.
   [4] https://hub.docker.com/_/registry
   [5] https://distribution.github.io/distribution/storage-drivers/s3


"""

# =========================================================================== #
import base64
import json
from typing import Any, Dict

import httpx
import pulumi
import pulumi_kubernetes as k8s
import pulumi_linode as linode
from pulumi import Config, Output, ResourceOptions, warn
from pulumi_kubernetes.core.v1 import SecretVolumeSourceArgs
from pulumi_kubernetes.helm.v3.helm import FetchOpts
from pulumi_kubernetes.meta.v1 import LabelSelectorArgs
from rich.console import Console

# --------------------------------------------------------------------------- #
from captura_pulumi import porkbun, util
from captura_pulumi.porkbun import PorkbunRequests, handle_porkbun

# NOTE: These kubernetes object names are constants for ease of lookup.
RE_SUBDOMAIN = "(?:[A-Za-z0-9\\-]{0,61}[A-Za-z0-9])?"
ERROR_PAGES = "error-pages"

TRAEFIK_API_VERSION = "traefik.io/v1alpha1"
TRAEFIK_NAMESPACE = "traefik"
TRAEFIK_RELEASE = "traefik"
TRAEFIK_MW_BASICAUTH = "traefik-basicauth"
TRAEFIK_MW_RATELIMIT = "traefik-ratelimit"
TRAEFIK_MW_REDIRECT_WILDCARD = "traefik-redirect-wildcard"
TRAEFIK_MW_REGISTRY_HEADERS = "traefik-registry-headers"
TRAEFIK_MW_ERROR_PAGES = "traefik-error-pages"
TRAEFIK_MW_REQUIRED = "traefik-required"
TRAEFIK_INGRESSROUTE_DEFAULT = "traefik-default"
TRAEFIK_MW_CIRCUIT_BREAKER = "traefik-circuit-breaker"

REGISTRY_NAMESPACE = "registry"
REGISTRY_RELEASE = "registry"
REGISTRY_PORT = 5000

CAPTURA_NAMESPACE = "captura"
CAPTURA_PORT = 8080
CAPTURA_PORT_SERVICE = 80
CAPTURA_TEXT_GIT_SYNC_REPO = "captura-text-portfolio-assets.git"
CAPTURA_TEXT_GIT_SYNC = "captura-git-sync"
CAPTURA_TEXT_DOCS = "/home/captura/app/plugins/acederbergio"


def create_traefik(config: Config) -> k8s.helm.v3.Chart:

    # NOTE: Since visibility of traefik does not really matter here and since
    #       hooks might be necessary, and further since releases use the built
    #       in functionality of helm to create the release - I would rather use
    #       the release resource. For more on the difference, see [2].
    _ = k8s.core.v1.Namespace("traefik-namespace", metadata=dict(name="traefik"))
    porkbun = PorkbunRequests.from_config(config)

    _ = k8s.core.v1.Secret(
        "traefik-secret-porkbun",
        metadata=create_metadata("traefik-porkbun"),
        string_data={
            "porkbun_api_key": porkbun.api_key,
            "porkbun_secret_key": porkbun.secret_key,
        },
    )

    # NOTE: extraObjects is not allowed since it usually results in lifecycle
    #       issues.
    traefik_values = util.load(
        util.path.asset("helm/traefik-values.yaml"),
        exclude=dict(
            extraObjects=None,
            ingressRoute=dict(dashboard=dict(enabled=False)),
            env=[
                # NOTE: Fields should match those provided in the first secret in
                #       extra objects.
                {
                    "name": "PORKBUN_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "traefik-porkbun",
                            "key": "porkbun_api_key",
                        }
                    },
                },
                {
                    "name": "PORKBUN_SECRET_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "traefik-porkbun",
                            "key": "porkbun_secret_key",
                        }
                    },
                },
            ],
        ),
    )
    assert "extraObjects" not in traefik_values

    traefik_chart = k8s.helm.v3.Chart(
        "traefik",
        k8s.helm.v3.ChartOpts(
            chart="traefik",
            fetch_opts=k8s.helm.v3.FetchOpts(repo="https://traefik.github.io/charts"),
            namespace=TRAEFIK_NAMESPACE,
            values=traefik_values,
        ),
    )

    Output.all(
        config.require("domain"),
        traefik_chart.resources,
        traefik_chart.ready,
    ).apply(lambda d: handle_porkbun_traefik(*d[:2]))

    return traefik_chart


def handle_porkbun_traefik(domain: str, resources):
    # NOTE: Resource keys are structured like {apiVersion}/{kind}:{namespace/name}
    traefik_service = resources[f"v1/Service:{TRAEFIK_NAMESPACE}/traefik"]
    ip = traefik_service.status.load_balancer.ingress[0].ip
    ip.apply(lambda ipaddr: handle_porkbun(domain=domain, ipaddr=ipaddr))


def create_error_pages(config: pulumi.Config):
    domain = config.require("domain")
    labels = util.create_labels(
        domain,
        tier=util.LabelTier.base,
        component=util.LabelComponent.error_pages,
        from_="pulumi",
    )

    selector = k8s.meta.v1.LabelSelectorArgs(match_labels=labels)
    metadata = k8s.meta.v1.ObjectMetaArgs(
        name="error-pages", namespace=TRAEFIK_NAMESPACE, labels=labels
    )
    show_details = config.get_bool("error_pages_show_details", True)

    port = 8080
    container_args = k8s.core.v1.ContainerArgs(
        name="error-pages",
        image="ghcr.io/tarampampam/error-pages",
        readiness_probe=k8s.core.v1.ProbeArgs(
            http_get=k8s.core.v1.HTTPGetActionArgs(
                path="/500.html",
                port=port,
            )
        ),
        ports=[k8s.core.v1.ContainerPortArgs(container_port=port)],
        env=[  # type: ignore
            dict(name="SHOW_DETAILS", value=str(1 if show_details else 0)),
            dict(
                name="TEMPLATE_NAME",
                value=config.get(
                    "error_pages_template_name",
                    "https://tarampampam.github.io/error-pages/",
                ),
            ),
        ],
    )

    deployment = k8s.apps.v1.Deployment(
        "error-pages-deployment",
        metadata=metadata,
        spec=k8s.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=selector,
            template=k8s.core.v1.PodTemplateSpecArgs(
                metadata=metadata,
                spec=k8s.core.v1.PodSpecArgs(
                    containers=[container_args],
                ),
            ),
        ),
    )

    service = k8s.core.v1.Service(
        "error-pages-service",
        metadata=metadata,
        spec=k8s.core.v1.ServiceSpecArgs(
            type="ClusterIP",
            selector=labels,
            ports=[
                k8s.core.v1.ServicePortArgs(
                    name="error-pages-http",
                    port=port,
                    target_port=port,
                )
            ],
        ),
    )


def create_registry(
    config: pulumi.Config,
    *,
    access_key: str,
    secret_key: str,
    cluster: str,
    endpoint: str,
    label: str,
) -> k8s.helm.v3.Chart:
    # NOTE: https://github.com/opencontainers/distribution-spec
    # NOTE: https://github.com/distribution/distribution

    k8s.core.v1.Namespace("registry-namespace", metadata=dict(name=REGISTRY_NAMESPACE))

    # k8s.core.v1.Secret(
    #     "registry-s3-secret",
    #     metadata=create_metadata(REGISTRY_NAMESPACE + "-s3"),
    #     string_data={
    #         "accessKey": access_key,
    #         "secretKey": secret_key,
    #     },
    # )

    # NOTE: The haSharedSecret field is required to get the secret to not
    #       be replaced every time pulumi up is run (because it is otherwise
    #       a random value, see [3].
    # NOTE: Generating the htpasswd is a pain in the ass. Do
    #
    #       .. code:: sh
    #
    #          HTPASSWD_OUT=$( htpasswd -nbB username password )
    #          pulumi config --secret registry_htpasswd HTPASSWD_OUT
    #
    # registry_htpasswd = config.require_secret("registry_htpasswd")
    registry_hasharedsecret = config.require_secret("registry_hasharedsecret")

    domain = config.require("domain")
    host = f"registry.{domain}"
    registry_values = util.load(
        util.path.asset("helm/registry-values.yaml"),
        exclude={
            "secrets": {
                "haSharedSecret": registry_hasharedsecret,
                # "htpasswd": registry_htpasswd,
                "s3": {"accessKey": access_key, "secretKey": secret_key},
            },
            "s3": {
                "region": cluster,
                "regionEndpoint": endpoint,
                "secure": True,
                "bucket": label,
            },
        },
        overwrite=dict(
            configData=dict(http=dict(relativeurls=True, host=f"https://{host}"))
        ),
    )
    # Console().print_json(json.dumps(registry_values, default=str))
    # assert False

    registry = k8s.helm.v3.Chart(
        "registry",
        k8s.helm.v3.ChartOpts(
            chart="docker-registry",
            fetch_opts=k8s.helm.v3.FetchOpts(
                repo="https://helm.twun.io",
            ),
            namespace=REGISTRY_NAMESPACE,
            values=registry_values,
        ),
    )

    labels = util.create_labels(
        domain,
        tier=util.LabelTier.base,
        component=util.LabelComponent.error_pages,
        from_="pulumi",
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-registry-headers",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_REGISTRY_HEADERS, labels=labels),
        spec={
            "headers": {
                "customRequestHeaders": {
                    "Docker-Distribution-Api-Version": "registry/2.0"
                }
            }
        },
    )

    id = f"v1/Service:{REGISTRY_NAMESPACE}/registry-docker-registry"
    service = registry.resources[id]
    routes = [
        {
            "kind": "Rule",
            "match": f"HOST(`{host}`)",
            "middlewares": [
                # NOTE: Somehow these commented middlewares break login. WTF!
                #       DO NOT UNCOMMENT OR DELETE THIS! IT TOOK SO LONG TO
                #       FIND THIS BUG!
                #
                # .. code:: sh
                #
                #   {
                #       "name": TRAEFIK_MW_REQUIRED,
                #       "namespace": TRAEFIK_NAMESPACE,
                #   },
                #   {
                #       "name": TRAEFIK_MW_ERROR_PAGES,
                #       "namespace": TRAEFIK_NAMESPACE,
                #   },
                {
                    "name": TRAEFIK_MW_REGISTRY_HEADERS,
                    "namespace": TRAEFIK_NAMESPACE,
                },
                {
                    "name": TRAEFIK_MW_BASICAUTH,
                    "namespace": TRAEFIK_NAMESPACE,
                },
            ],
            "services": [
                {
                    "name": service.metadata.name,
                    "kind": "Service",
                    "namespace": service.metadata.namespace,
                    "port": REGISTRY_PORT,
                }
            ],
        }
    ]
    k8s.apiextensions.CustomResource(
        "registry-ingressroute",
        api_version=TRAEFIK_API_VERSION,
        kind="IngressRoute",
        metadata=create_metadata(
            REGISTRY_RELEASE,
            REGISTRY_NAMESPACE,
            labels=labels,
        ),
        spec={
            "entryPoints": ["websecure"],
            "routes": routes,
            "tls": {"certResolver": "letsencrypt"},
        },
        opts=ResourceOptions(depends_on=service),
    )

    return registry


def create_metadata(v: str, namespace: str | None = None, **kwargs):
    kwargs.update(name=v, namespace=namespace or TRAEFIK_NAMESPACE)
    return kwargs


def create_traefik_ingressroutes(config: pulumi.Config):
    # --------------------------------------------------------------- #
    # Middlewares.
    domain = config.require("domain")
    labels = util.create_labels(
        domain,
        tier=util.LabelTier.base,
        component=util.LabelComponent.traefik,
        from_="pulumi",
    )

    # NOTE: For dashboard login. No ingressRoute yet.
    traefik_dash_un = config.require("traefik_dashboard_username")
    traefik_dash_pw = config.require_secret("traefik_dashboard_password")
    k8s.core.v1.Secret(
        "traefik-secret-dashboard-basicauth",
        metadata={
            "name": TRAEFIK_MW_BASICAUTH,
            "namespace": "traefik",
        },
        type="kubernetes.io/basic-auth",
        string_data={
            "username": traefik_dash_un,
            "password": traefik_dash_pw,
        },
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-error-pages",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_ERROR_PAGES, labels=labels),
        spec={
            "errors": {
                "status": ["400-499", "500-599"],
                "query": "/{status}.html",
                "service": {
                    "namespace": TRAEFIK_NAMESPACE,
                    "name": ERROR_PAGES,
                    "port": 8080,
                },
            }
        },
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-dashboard-auth",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_BASICAUTH, labels=labels),
        spec={"basicAuth": {"secret": TRAEFIK_MW_BASICAUTH}},
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-dashboard-ratelimit",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_RATELIMIT, labels=labels),
        spec={"rateLimit": {"average": 100, "burst": 200}},
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-redirect-wildcard",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_REDIRECT_WILDCARD, labels=labels),
        spec={
            "redirectRegex": {
                "regex": f"^https?://{RE_SUBDOMAIN}.acederberg.io(/.*)?",
                "replacement": "https://acederberg.io${1}",
            }
        },
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-circuit-breaker",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_CIRCUIT_BREAKER),
        spec={
            "circuitBreaker": {
                "expression": "ResponseCodeRatio(500, 600, 0, 600) > 0.15"
            }
        },
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-required",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_REQUIRED),
        spec={
            "chain": {
                "middlewares": [
                    {"name": TRAEFIK_MW_RATELIMIT},
                    {"name": TRAEFIK_MW_CIRCUIT_BREAKER},
                ]
            }
        },
    )

    domain = config.require("domain")
    routes = [
        {
            "kind": "Rule",
            "priority": 1,
            "match": "HOST(`acederberg.io`)",
            "middlewares": [
                {"name": TRAEFIK_MW_REQUIRED},
                {"name": TRAEFIK_MW_ERROR_PAGES},
            ],
            "services": [
                error_pages := {
                    "name": ERROR_PAGES,
                    "kind": "Service",
                    "namespace": TRAEFIK_NAMESPACE,
                    "port": 8080,
                }
            ],
        },
        {
            "kind": "Rule",
            "priority": 1,
            "match": f"HOSTREGEXP(`{RE_SUBDOMAIN}.acederberg.io`)",
            "middlewares": [{"name": TRAEFIK_MW_REDIRECT_WILDCARD}],
            "services": [error_pages],
        },
        {
            "kind": "Rule",
            "priority": 3,
            "match": f"HOST(`errors.{domain}`)",
            "middlewares": [
                {"name": TRAEFIK_MW_REQUIRED},
                {"name": TRAEFIK_MW_ERROR_PAGES},
            ],
            "services": [error_pages],
        },
        {
            "kind": "Rule",
            "priority": 2,
            "match": f"HOST(`traefik.{domain}`)",
            "middlewares": [
                {"name": TRAEFIK_MW_BASICAUTH},
                {"name": TRAEFIK_MW_REQUIRED},
                {"name": TRAEFIK_MW_ERROR_PAGES},
            ],
            "services": [{"name": "api@internal", "kind": "TraefikService"}],
            "kind": "Rule",
        },
    ]

    # NOTE: Because traefik misconfiguration results in letsencrypt rate limit
    #       issues.
    if config.require_bool("traefik_include_ingressroutes"):
        k8s.apiextensions.CustomResource(
            "traefik-ingressroute-default",
            api_version=TRAEFIK_API_VERSION,
            kind="IngressRoute",
            metadata=create_metadata(TRAEFIK_INGRESSROUTE_DEFAULT, labels=labels),
            spec={
                "entryPoints": ["websecure"],
                "routes": routes,
                "tls": {"certResolver": "letsencrypt"},
            },
        )


# WARNING: Uninstalling the operator is a huge pain in the ass.

# MYSQL_NAMESPACE = "mysql"
#
#
# def create_mysql(config: pulumi.Config):
#
#     mysql_values = util.load(
#         util.path.asset("helm/mysql.yaml"),
#         exclude=dict(
#         ),
#     )
#     k8s.helm.v3.Chart(
#         "mysql",
#         k8s.helm.v3.ChartOpts(
#             chart="mysql",
#             fetch_opts=k8s.helm.v3.FetchOpts(
#                 repo="https://mysql.github.io/mysql-operator"
#             ),
#             namespace=MYSQL_NAMESPACE,
#             values=mysql_values,
#         ),
#     )


def create_captura(config: pulumi.Config):

    domain = config.require("domain")
    labels = util.create_labels(
        domain,
        tier=util.LabelTier.api,
        component=util.LabelComponent.traefik,
        from_="pulumi",
    )
    metadata = create_metadata(
        "captura",
        CAPTURA_NAMESPACE,
        labels=labels,
    )

    # NOTE: Make the secret like
    # CAPTURA_CONFIG_B64=$( cat ./config/app.prod.yaml | base64 --encode )
    # pulumi config set --secret captura_config_b64 $CAPTURA_CONFIG_B64
    captura_config_b64 = config.require_secret("captura_config_b64")
    text_status_b64 = config.require_secret("captura_text_status_b64")
    text_config_b64 = config.require_secret("captura_text_config_b64")
    k8s.core.v1.Namespace(CAPTURA_NAMESPACE, metadata=metadata)
    k8s.core.v1.Secret(
        "captura-secret",
        metadata=metadata,
        data={
            "app.yaml": captura_config_b64,
            "text.status.yaml": text_status_b64,
            "text.yaml": text_config_b64,
        },
    )

    # NOTE: Use an init container to clone in browser content.
    volume_args = k8s.core.v1.VolumeArgs(
        name="captura",
        secret=k8s.core.v1.SecretVolumeSourceArgs(
            secret_name="captura",
        ),
    )
    # volume_args_git_sync = k8s.core.v1.VolumeArgs(
    #     name=CAPTURA_TEXT_GIT_SYNC, empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()
    # )
    # init_container_args = k8s.core.v1.ContainerArgs(
    #     name=CAPTURA_TEXT_GIT_SYNC,
    #     image="registry.k8s.io/git-sync/git-sync:v4.2.3",
    #     volume_mounts=[
    #         k8s.core.v1.VolumeMountArgs(
    #             name=CAPTURA_TEXT_GIT_SYNC,
    #             mount_path="/git",
    #         ),
    #     ],
    #     args=[
    #         f"--repo=https://github.com/acederberg/{CAPTURA_TEXT_GIT_SYNC_REPO}",
    #         "--depth=1",
    #         "--ref=master",
    #         "--root=/git",
    #         "--one-time",
    #     ],
    # )
    container_args = k8s.core.v1.ContainerArgs(
        name="captura",
        image="acederberg/acederberg-portfolio:5ec5fa09ea1a67ba7bda79a116a443105bf32d78",
        image_pull_policy="Always",
        ports=[k8s.core.v1.ContainerPortArgs(container_port=8080)],
        env=[
            k8s.core.v1.EnvVarArgs(
                name="CAPTURA_TEXT_DOCS",
                value=CAPTURA_TEXT_DOCS,
            ),
        ],
        volume_mounts=[
            # k8s.core.v1.VolumeMountArgs(
            #     name=CAPTURA_TEXT_GIT_SYNC,
            #     mount_path=CAPTURA_TEXT_DOCS,
            #     sub_path=CAPTURA_TEXT_GIT_SYNC_REPO,
            # ),
            k8s.core.v1.VolumeMountArgs(
                name="captura",
                mount_path="/home/captura/.captura/app.yaml",
                sub_path="app.yaml",
            ),
            k8s.core.v1.VolumeMountArgs(
                name="captura",
                mount_path="/home/captura/.captura/text.status.yaml",
                sub_path="text.status.yaml",
            ),
            k8s.core.v1.VolumeMountArgs(
                name="captura",
                mount_path="/home/captura/.captura/text.yaml",
                sub_path="text.yaml",
            ),
        ],
        readiness_probe=k8s.core.v1.ProbeArgs(
            failure_threshold=3,
            http_get=k8s.core.v1.HTTPGetActionArgs(
                path="/",
                port=CAPTURA_PORT,
                scheme="HTTP",
            ),
            period_seconds=10,
            success_threshold=1,
            timeout_seconds=1,
        ),
    )

    k8s.apps.v1.Deployment(
        "captura-deployment",
        metadata=metadata,
        spec=k8s.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=LabelSelectorArgs(match_labels=labels),
            template=k8s.core.v1.PodTemplateSpecArgs(
                metadata=k8s.meta.v1.ObjectMetaArgs(**metadata),
                spec=k8s.core.v1.PodSpecArgs(
                    # init_containers=[init_container_args],
                    containers=[container_args],
                    volumes=[volume_args],  # , volume_args_git_sync],
                ),
            ),
        ),
    )

    service = k8s.core.v1.Service(
        "captura-service",
        metadata=metadata,
        spec=k8s.core.v1.ServiceSpecArgs(
            type="ClusterIP",
            selector=labels,
            ports=[
                k8s.core.v1.ServicePortArgs(
                    name="captura",
                    port=CAPTURA_PORT_SERVICE,
                    target_port=CAPTURA_PORT,
                )
            ],
        ),
    )

    host = f"captura.{domain}"
    k8s.apiextensions.CustomResource(
        "traefik-mw-registry-headers",
        api_version=TRAEFIK_API_VERSION,
        kind="Middleware",
        metadata=create_metadata(
            "traefik-add-prefix-text",
            TRAEFIK_NAMESPACE,
            labels=labels,
        ),
        # spec=dict(addPrefix=dict(prefix="/text")),
        spec=dict(
            redirectRegex=dict(
                regex="^https://acederberg.io/(.*)",
                replacement="https://acederberg.io/text/${1}",
            ),
        ),
    )
    routes = [
        {
            "kind": "Rule",
            "match": f"HOST(`{host}`)",
            "services": [
                svc := {
                    "name": "captura",
                    "kind": "Service",
                    "namespace": CAPTURA_NAMESPACE,
                    "port": CAPTURA_PORT_SERVICE,
                },
            ],
            "middlewares": (
                mw := [
                    {
                        "name": TRAEFIK_MW_RATELIMIT,
                        "namespace": TRAEFIK_NAMESPACE,
                    },
                    {
                        "name": TRAEFIK_MW_ERROR_PAGES,
                        "namespace": TRAEFIK_NAMESPACE,
                    },
                ]
            ),
        },
        {
            "kind": "Rule",
            "match": f"HOST(`{domain}`) && PathPrefix(`/text`)",
            "middlewares": mw,
            "services": [svc],
        },
        {
            "kind": "Rule",
            "match": f"HOST(`{domain}`) && !PathPrefix(`/text`)",
            "middlewares": [
                {
                    "name": "traefik-add-prefix-text",
                    "namespace": TRAEFIK_NAMESPACE,
                },
                *mw,
            ],
            "services": [svc],
        },
        {
            "kind": "Rule",
            "match": f"HOST(`{domain}`) && PathPrefix(`/colors/`)",
            "middlewares": [
                *mw,
            ],
            "services": [svc],
        },
    ]

    k8s.apiextensions.CustomResource(
        "registry-ingressroute",
        api_version=TRAEFIK_API_VERSION,
        kind="IngressRoute",
        metadata=metadata,
        spec={
            "entryPoints": ["websecure"],
            "routes": routes,
            "tls": {"certResolver": "letsencrypt"},
        },
        opts=ResourceOptions(depends_on=service),
    )
