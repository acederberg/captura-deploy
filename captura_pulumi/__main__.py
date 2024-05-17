# NOTE: Useful links
#
#       .. code:: txt
#
#          [1] LKE Firewall: https://www.linode.com/docs/products/compute/kubernetes/get-started/#general-network-and-firewall-information
#          LKE Not added to `captura-firewall`: https://www.linode.com/community/questions/19155/securing-k8s-cluster
#          Node types:   https://api.linode.com/v4/linode/types
#
# NOTE: Initially generated using pulumi AI (it did a shit job, not taking jobs
#       any time soon).
# =========================================================================== #
import functools
import itertools
from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s
import pulumi_linode as linode
from pulumi.output import Output
from pulumi_kubernetes.networking.v1 import Ingress
from pulumi_kubernetes_cert_manager import CertManager
from pulumi_linode.outputs import LkeClusterPool

# NOTE: Not going to bother writing out an actual configuration for now.
#       To learn more do ``linode-cli linode types``.
CLUSTER_K8S_VERSION = "1.29"
CLUSTER_DEFAULT_NODE_COUNT = 1
CLUSTER_NODE_TYPE = "g6-standard-1"
CLUSTER_TAGS = ["captura", "dev"]
CLUSTER_REGION = "us-west"

# --------------------------------------------------------------------------- #
# NOTE: Create a Linode Kubernetes cluster


cluster = linode.LkeCluster(
    "captura-cluster",
    linode.LkeClusterArgs(
        label="captura-cluster",
        k8s_version=CLUSTER_K8S_VERSION,
        region=CLUSTER_REGION,
        pools=[
            linode.LkeClusterPoolArgs(
                count=CLUSTER_DEFAULT_NODE_COUNT,
                type=CLUSTER_NODE_TYPE,
            )
        ],
        tags=CLUSTER_TAGS,
    ),
)


# NOTE: This should make it such that all devices attached cannot be accessed
#       using ssh directly. Somehow linode made it such that using lsh ssh is
#       still possible.
# NOTE: Enum values for fields are no always available through Pulumi docs, so
#       instead see the api docs:
#       https://www.linode.com/docs/api/networking/#firewall-rules-update
firewall = linode.Firewall(
    "captura-firewall",
    linode.FirewallArgs(
        label="captura-firewall-k8s",
        inbound_policy="DROP",
        outbound_policy="ACCEPT",
        inbounds=[
            # NOTE: Probably don't want because should just use the load
            #       balancer.
            # linode.FirewallInboundArgs(
            #     label="captura-firewall-http-inbound",
            #     action="ACCEPT",
            #     protocol="TCP",
            #     ports="80, 443",
            #     ipv4s=["0.0.0.0/0"],
            #     ipv6s=["::/0"],
            # ),
            # NOTE: For kublete health checks, wiregaurd tunneling, calico, and
            #       node ports.
            linode.FirewallInboundArgs(
                label="captura-firewall-internal",
                action="ACCEPT",
                protocol="TCP",
                ports="179, 10250, 30000-32767",
                ipv4s=["192.168.128.0/17"],
            ),
            linode.FirewallInboundArgs(
                label="captura-firewall-internal-udp",
                action="ACCEPT",
                protocol="UDP",
                ports="51820, 30000-32767",
                ipv4s=["192.168.128.0/17"],
            ),
            linode.FirewallInboundArgs(
                label="captura-firewall-imap-out",
                action="ACCEPT",
                protocol="IPENCAP",
                ipv4s=["192.168.128.0/17"],
            ),
        ],
        # NOTE: Tried setting these to no avail - in the forum post [2] the
        #       comment from stravostino says to allow all outbound.
        outbounds=[],
        tags=CLUSTER_TAGS,
    ),
)


def create_cluster_firewall_device(
    *, pools: Output[Sequence[linode.LkeNodePool]], id_firewall: int
):
    """Add pool nodes to the firewal"."""
    pool: dict
    # for pool in pools:
    #     assert isinstance(pool["nodes"], list)
    #     for node in pool["nodes"]:
    tuple(
        map(
            lambda node: linode.FirewallDevice(
                "captura-firewall-cluster-device",
                entity_id=int(node["instance_id"]),
                firewall_id=int(id_firewall),
                entity_type="linode",
            ),
            itertools.chain(*(p["nodes"] for p in pools)),
        )
    )


pulumi.Output.all(pools=cluster.pools, id_firewall=firewall.id).apply(
    lambda args: create_cluster_firewall_device(
        pools=args["pools"],
        id_firewall=args["id_firewall"],
    )
)

# --------------------------------------------------------------------------- #
# Deploy to k8s.

# Kubernetes provider to deploy resources into the cluster created on Linode
k8s_provider = k8s.Provider("k8s-provider", cluster=cluster.id)


# NOTE: Configuration of traefik will address ssl certificate automation with
#       acme.
# traefik_chart = k8s.helm.v3.Chart(
#     "traefik/traefik",
#     k8s.helm.v3.ChartOpts(chart="traefik", fetch_opts=k8s.helm.v3.ChartOpts(repo="")),
#     opts=pulumi.ResourceOptions(provider=k8s_provider),
# )

# NOTE: helm repo add https://traefik.github.io/charts
# traefik = k8s.helm.v3.Release(
#     "traefik",
#     chart="traefik",
#     namespace="traefik",
#     opts=pulumi.ResourceOptions(provider=k8s_provider),
# )

# # Create an IngressRoute for Traefik to use the ACME SSL certificate
# ingress_route = Ingress(
#     "ingress-route",
#     metadata=k8s.meta.ObjectMetaArgs(
#         labels={"app": "traefik"},
#     ),
#     spec=k8s.networking.v1.IngressSpecArgs(
#         ingress_class_name="traefik",
#         rules=[
#             k8s.networking.v1.IngressRuleArgs(
#                 host="www.example.com",
#                 http=k8s.networking.v1.HTTPIngressRuleValueArgs(
#                     paths=[
#                         k8s.networking.v1.HTTPIngressPathArgs(
#                             path="/",
#                             path_type="Prefix",
#                             backend=k8s.networking.v1.IngressBackendArgs(
#                                 service=k8s.networking.v1.IngressServiceBackendArgs(
#                                     name="traefik",
#                                     port=k8s.networking.v1.ServiceBackendPortArgs(
#                                         number=80,
#                                     ),
#                                 ),
#                             ),
#                         ),
#                     ],
#                 ),
#             ),
#         ],
#         tls=[
#             k8s.networking.v1.IngressTLSArgs(
#                 hosts=["www.example.com"],
#                 secret_name=acme_certificate.name,
#             ),
#         ],
#     ),
#     opts=pulumi.ResourceOptions(provider=k8s_provider),
# )
#
# # Export the Kubeconfig and Traefik service IP
# pulumi.export("kubeconfig", cluster.kube_config_raw)
# pulumi.export(
#     "traefik_service_ip",
#     traefik.status.apply(
#         lambda s: s.load_balancer.ingress[0].ip if s.load_balancer.ingress else None
#     ),
# )
