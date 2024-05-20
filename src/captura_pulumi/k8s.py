"""
.. code:: text

   [1] https://github.com/traefik/traefik-helm-chart/blob/master/EXAMPLES.md#use-traefik-native-lets-encrypt-integration-without-cert-manager
   [2] https://www.pulumi.com/registry/packages/kubernetes/how-to-guides/choosing-the-right-helm-resource-for-your-use-case/
"""

# =========================================================================== #
from typing import Any, Dict

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

    k8s_provider = k8s.Provider("k8s-provider", cluster=id_cluster)

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
        lambda namespace: handle_porkbun_traefik(
            config,
            namespace=namespace,
        )
    )
    return traefik_release


def handle_porkbun_traefik(
    config: Config,
    *,
    namespace: str | None,
):

    assert namespace is not None, "Namespace should not be `None`."
    traefik = k8s.core.v1.Service.get(
        "captura-traefik", Output.concat(f"{namespace}/traefik")
    )
    Output.all(
        config.get("domain"),
        # status of the service. Populated by the system. Read-only. More info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#spec-and-status
        traefik.status.load_balancer.ingress[0].ip,
    ).apply(lambda data: porkbun.handle_porkbun(domain=data[0], ipaddr=data[1]))
