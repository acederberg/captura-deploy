"""
.. code:: text

   [1] https://github.com/traefik/traefik-helm-chart/blob/master/EXAMPLES.md#use-traefik-native-lets-encrypt-integration-without-cert-manager
   [2] https://www.pulumi.com/registry/packages/kubernetes/how-to-guides/choosing-the-right-helm-resource-for-your-use-case/
"""

# =========================================================================== #
from typing import Any, Dict

import pulumi
import pulumi_kubernetes as k8s
import pulumi_linode as linode
from pulumi import Config, Output
from pulumi_kubernetes.helm.v3.helm import FetchOpts

# --------------------------------------------------------------------------- #
from captura_pulumi import porkbun, util
from captura_pulumi.porkbun import PorkbunRequests

# NOTE: These kubernetes object names are constants for ease of lookup.
RE_SUBDOMAIN = "(?:[A-Za-z0-9\\-]{0,61}[A-Za-z0-9])?"
ERROR_PAGES = "error-pages"
TRAEFIK_NAMESPACE = "traefik"
TRAEFIK_RELEASE = "traefik"
TRAEFIK_MW_DASHBOARD_BASICAUTH = "traefik-dashboard-basicauth"
TRAEFIK_MW_RATELIMIT = "traefik-ratelimit"
TRAEFIK_MW_REDIRECT_WILDCARD = "traefik-redirect-wildcard"
TRAEFIK_MW_ERROR_PAGES = "traefik-error-pages"
TRAEFIK_MW_REQUIRED = "traefik-required"
TRAEFIK_INGRESSROUTE_DEFAULT = "traefik-default"
TRAEFIK_MW_CIRCUIT_BREAKER = "traefik-circuit-breaker"


def create_traefik_values(config: Config) -> Dict[str, Any]:
    # NOTE: Configuration of traefik will address ssl certificate automation with
    #       acme.
    traefik_values_path = util.path.asset("helm/traefik-values.yaml")
    traefik_values = util.load(traefik_values_path)

    # NOTE: These requirements are derivative of result of the example in [1]
    if len(
        bad := {
            field
            for field in {"extraObjects", "env", "IngressRoute"}
            if traefik_values.get(field) is not None
        }
    ):
        msg_fmt = "Helm values must not specify `{}`."
        raise ValueError(msg_fmt.join(bad))

    # NOTE: For dashboard login. No ingressRoute yet. Was in extraObjects, but
    #       caused lifecycle issues.
    traefik_dash_un = config.require("traefik_dashboard_username")
    traefik_dash_pw = config.require_secret("traefik_dashboard_password")
    k8s.core.v1.Secret(
        "traefik-secret-dashboard-basicauth",
        metadata={
            "name": TRAEFIK_MW_DASHBOARD_BASICAUTH,
            "namespace": "traefik",
        },
        type="kubernetes.io/basic-auth",
        string_data={
            "username": traefik_dash_un,
            "password": traefik_dash_pw,
        },
    )

    traefik_values.update(
        # NOTE: Wait until porkbun has updated dns.
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
    )

    return traefik_values


def create_traefik(config: Config, *, id_cluster: str) -> k8s.helm.v3.Release:

    k8s.Provider("k8s-provider", cluster=id_cluster)

    # NOTE: Since visibility of traefik does not really matter here and since
    #       hooks might be necessary, and further since releases use the built
    #       in functionality of helm to create the release - I would rather use
    #       the release resource. For more on the difference, see [2].
    _ = k8s.core.v1.Namespace("traefik", metadata=dict(name="traefik"))
    porkbun = PorkbunRequests.from_config(config)

    _ = k8s.core.v1.Secret(
        "traefik-secret-porkbun",
        metadata=create_metadata("traefik-porkbun"),
        string_data={
            "porkbun_api_key": porkbun.api_key,
            "porkbun_secret_key": porkbun.secret_key,
        },
    )

    traefik_release = k8s.helm.v3.Release(
        "captura-traefik",
        k8s.helm.v3.ReleaseArgs(
            name="traefik",
            chart="traefik",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://traefik.github.io/charts",
            ),
            namespace=TRAEFIK_NAMESPACE,
            values=create_traefik_values(config),
        ),
    )

    traefik_release.id.apply(lambda _: handle_porkbun_traefik(config))
    return traefik_release


def handle_porkbun_traefik(config: Config) -> None:

    release_name = TRAEFIK_NAMESPACE + "/" + TRAEFIK_RELEASE
    traefik = k8s.core.v1.Service.get("captura-traefik", release_name)
    Output.all(
        domain := config.require("domain"),
        traefik.status.load_balancer.ingress[0].ip,
    ).apply(lambda data: porkbun.handle_porkbun(domain=data[0], ipaddr=data[1]))


def create_error_pages(config: pulumi.Config, *, namespace: str):
    domain = config.require("domain")
    labels = {
        f"{domain}/tier": "base",
        f"{domain}/from": "pulumi",
        f"{domain}/component": "error-pages",
    }

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


def create_metadata(v: str, **kwargs):
    kwargs.update(name=v, namespace="traefik")
    return kwargs


def create_traefik_ingressroutes(config: pulumi.Config, *, namespace: str):
    # --------------------------------------------------------------- #
    # Middlewares.
    domain = config.require("domain")
    labels = {
        f"{domain}/tier": "base",
        f"{domain}/from": "pulumi",
        f"{domain}/component": "traefik",
    }

    k8s.apiextensions.CustomResource(
        "traefik-mw-error-pages",
        api_version="traefik.io/v1alpha1",
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_ERROR_PAGES, labels=labels),
        spec={
            "errors": {
                "status": ["400-499", "500-599"],
                "query": "/{status}.html",
                "service": {
                    "namespace": namespace,
                    "name": ERROR_PAGES,
                    "port": 8080,
                },
            }
        },
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-dashboard-auth",
        api_version="traefik.io/v1alpha1",
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_DASHBOARD_BASICAUTH, labels=labels),
        spec={"basicAuth": {"secret": TRAEFIK_MW_DASHBOARD_BASICAUTH}},
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-dashboard-ratelimit",
        api_version="traefik.io/v1alpha1",
        kind="Middleware",
        metadata=create_metadata(TRAEFIK_MW_RATELIMIT, labels=labels),
        spec={"rateLimit": {"average": 100, "burst": 200}},
    )

    k8s.apiextensions.CustomResource(
        "traefik-mw-redirect-wildcard",
        api_version="traefik.io/v1alpha1",
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
        api_version="traefik.io/v1alpha1",
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
        api_version="traefik.io/v1alpha1",
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
                {"name": TRAEFIK_MW_DASHBOARD_BASICAUTH},
                {"name": TRAEFIK_MW_REQUIRED},
                {"name": TRAEFIK_MW_ERROR_PAGES},
            ],
            "services": [{"name": "api@internal", "kind": "TraefikService"}],
            "kind": "Rule",
        },
    ]

    k8s.apiextensions.CustomResource(
        "traefik-ingressroute-default",
        api_version="traefik.io/v1alpha1",
        kind="IngressRoute",
        metadata=create_metadata(TRAEFIK_INGRESSROUTE_DEFAULT, labels=labels),
        spec={
            "entryPoints": ["websecure"],
            "routes": routes,
            "tls": {"certResolver": "letsencrypt"},
        },
    )
