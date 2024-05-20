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
from captura_pulumi import util
from captura_pulumi.porkbun import PorkbunRequests


def create_traefik_values(config: Config) -> Dict[str, Any]:
    # NOTE: Configuration of traefik will address ssl certificate automation with
    #       acme.
    traefik_values_path = util.path.asset("helm/traefik-values.yaml")
    traefik_values = util.load(traefik_values_path)

    # NOTE: These requirements are derivative of result of the example in [1]
    if any(traefik_values.get(f) is not None for f in {"extraObjects", "env"}):
        raise ValueError("Helm values must not specify `extraObjects`.")

    traefik_dash_un = config.require("traefik_dashboard_username")
    traefik_dash_pw = config.require_secret("traefik_dashboard_password")

    traefik_values.update(
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
        extra_objects={
            "extraObjects": [
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
            ]
        },
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
    return traefik_release
