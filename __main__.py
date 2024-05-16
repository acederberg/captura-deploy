# NOTE: Gemerated using pulumi AI
import pulumi
import pulumi_kubernetes as k8s
import pulumi_linode as linode
from pulumi_kubernetes.networking.v1 import Ingress
from pulumi_kubernetes_cert_manager import CertManager
from pulumi_kubernetes_cert_manager.acme import Certificate

# Configure Kubernetes version, default node count and node type
k8s_version = "1.29"
default_node_count = 3  # Hardcoded for example, make this configurable as needed
node_type = "g6-standard-1"  # Hardcoded for example, make this configurable as needed

# --------------------------------------------------------------------------- #
# NOTE: Create a Linode Kubernetes cluster

cluster = linode.LkeCluster(
    "k8s-cluster",
    k8s_version=k8s_version,
    region="us-east",
    node_pools=[
        linode.LkeClusterNodePoolArgs(
            count=default_node_count,
            type=node_type,
        )
    ],
)

# Create Linode firewall with reasonable rules for Kubernetes
firewall = linode.Firewall(
    "k8s-firewall",
    devices=[
        linode.FirewallDeviceArgs(
            id=cluster.id,
            type="lke",
        )
    ],
    inbound=[
        linode.FirewallInboundRuleArgs(
            protocol="TCP",
            ports="80,443,8080,6443",  # Ports for HTTP, HTTPS, k8s api server
            ipv4=["0.0.0.0/0"],
            ipv6=["::/0"],
        )
    ],
)

# --------------------------------------------------------------------------- #
# Deploy to k8s.

# Kubernetes provider to deploy resources into the cluster created on Linode
k8s_provider = k8s.Provider("k8s-provider", kubeconfig=cluster.kube_config_raw)

# Deploy cert-manager into the Kubernetes cluster
cert_manager = CertManager(
    "cert-manager", opts=pulumi.ResourceOptions(provider=k8s_provider)
)

# Deploy an ACME SSL certificate using cert-manager
acme_certificate = Certificate(
    "acme-cert",
    common_name="example.com",
    dns_names=["www.example.com"],
    issuer_ref=k8s.apiextensions.CustomResourceRefArgs(
        group="cert-manager.io",
        kind="ClusterIssuer",
        name="letsencrypt-prod",
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# Deploy a Traefik service in the cluster
traefik = k8s.helm.v3.Release(
    "traefik",
    chart="traefik",
    namespace="default",
    values={
        "service": {
            "annotations": {
                "service.beta.kubernetes.io/linode-loadbalancer-throttle": "4",
            },
        },
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# Create an IngressRoute for Traefik to use the ACME SSL certificate
ingress_route = Ingress(
    "ingress-route",
    metadata=k8s.meta.ObjectMetaArgs(
        labels={"app": "traefik"},
    ),
    spec=k8s.networking.v1.IngressSpecArgs(
        ingress_class_name="traefik",
        rules=[
            k8s.networking.v1.IngressRuleArgs(
                host="www.example.com",
                http=k8s.networking.v1.HTTPIngressRuleValueArgs(
                    paths=[
                        k8s.networking.v1.HTTPIngressPathArgs(
                            path="/",
                            path_type="Prefix",
                            backend=k8s.networking.v1.IngressBackendArgs(
                                service=k8s.networking.v1.IngressServiceBackendArgs(
                                    name="traefik",
                                    port=k8s.networking.v1.ServiceBackendPortArgs(
                                        number=80,
                                    ),
                                ),
                            ),
                        ),
                    ],
                ),
            ),
        ],
        tls=[
            k8s.networking.v1.IngressTLSArgs(
                hosts=["www.example.com"],
                secret_name=acme_certificate.name,
            ),
        ],
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# Export the Kubeconfig and Traefik service IP
pulumi.export("kubeconfig", cluster.kube_config_raw)
pulumi.export(
    "traefik_service_ip",
    traefik.status.apply(
        lambda s: s.load_balancer.ingress[0].ip if s.load_balancer.ingress else None
    ),
)

