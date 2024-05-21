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

    traefik_dash_un = config.require("traefik_dashboard_username")
    traefik_dash_pw = config.require_secret("traefik_dashboard_password")

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
        # NOTE: For dashboard login. No ingressRoute yet.
        extraObjects=[
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "traefik-dashboard-auth",
                    "namespace": "traefik",
                },
                "type": "kubernetes.io/basic-auth",
                "stringData": {
                    "username": traefik_dash_un,
                    "password": traefik_dash_pw,
                },
            },
            {
                "apiVersion": "traefik.io/v1alpha1",
                "kind": "Middleware",
                "metadata": {
                    "name": "traefik-dashboard-auth",
                    "namespace": "traefik",
                },
                "spec": {"basicAuth": {"secret": "traefik-dashboard-auth"}},
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
        "treafik-porkbun",
        metadata={
            "name": "traefik-porkbun",
            "namespace": "traefik",
        },
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
            namespace="traefik",
            values=create_traefik_values(config),
        ),
    )
    traefik_release.namespace.apply(
        lambda ns: handle_porkbun_traefik(config, namespace=ns)
    )
    return traefik_release


def handle_porkbun_traefik(
    config: Config,
    *,
    namespace: str | None,
) -> None:

    assert namespace is not None, "Namespace should not be `None`."
    traefik = k8s.core.v1.Service.get(
        "captura-traefik", Output.concat(f"{namespace}/traefik")
    )
    Output.all(
        domain := config.require("domain"),
        traefik.status.load_balancer.ingress[0].ip,
    ).apply(lambda data: porkbun.handle_porkbun(domain=data[0], ipaddr=data[1]))

    k8s.apiextensions.CustomResource(
        "traefik-dashboard",
        api_version="traefik.io/v1alpha1",
        kind="IngressRoute",
        metadata={
            "namespace": namespace,
            "name": "traefik-dashboard",
        },
        spec={
            "entryPoints": ["websecure"],
            "routes": [
                {
                    "kind": "Rule",
                    "match": f"HOST(`traefik.{domain}`)",
                    "middlewares": [
                        {
                            "name": "traefik-dashboard-auth",
                            "namespace": namespace,
                        }
                    ],
                    "services": [
                        {
                            "name": "api@internal",
                            "kind": "TraefikService",
                        }
                    ],
                    "kind": "Rule",
                },
            ],
            "tls": {"certResolver": "letsencrypt"},
        },
    )


def create_error_pages(config: pulumi.Config, *, namespace: str):
    labels = {
        "acederberg.io/tier": "base",
        "acederberg.io/from": "pulumi",
        "acederberg.io/component": "error-pages",
    }

    selector = k8s.meta.v1.LabelSelectorArgs(match_labels=labels)
    metadata = k8s.meta.v1.ObjectMetaArgs(
        name="error-pages", namespace="traefik", labels=labels
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

    middleware = k8s.apiextensions.CustomResource(
        "error-pages-middleware",
        api_version="traefik.io/v1alpha1",
        kind="Middleware",
        metadata=metadata,
        spec={
            "errors": {
                "status": ["400-499", "500-599"],
                "query": "/{status}.html",
                "service": {
                    "namespace": namespace,
                    "name": service.metadata.name,
                    "port": port,
                },
            }
        },
    )

    ingress_route = k8s.apiextensions.CustomResource(
        "error-pages-ingressroute",
        api_version="traefik.io/v1alpha1",
        kind="IngressRoute",
        metadata=metadata,
        spec={
            "entryPoints": ["websecure"],
            "routes": [
                {
                    "kind": "Rule",
                    "match": "HOST(`errors.acederberg.io`)",
                    "middlewares": [{"name": "error-pages", "namespace": "traefik"}],
                    "services": [
                        {
                            "kind": "Service",
                            "name": "error-pages",
                            "namespace": "traefik",
                            "port": 8080,
                        }
                    ],
                }
            ],
            "tls": {"certResolver": "letsencrypt"},
        },
    )
    return service
