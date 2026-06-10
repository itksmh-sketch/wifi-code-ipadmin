#!/bin/bash
# Run ONCE on the Google Cloud VM to set up the WireGuard server.
# Routers dial in to this server; the platform reaches each router at its tunnel IP.

set -e

# Install WireGuard
sudo apt update
sudo apt install -y wireguard wireguard-tools

# Generate server keypair
sudo mkdir -p /etc/wireguard
wg genkey | sudo tee /etc/wireguard/server_private.key | \
  wg pubkey | sudo tee /etc/wireguard/server_public.key
sudo chmod 600 /etc/wireguard/server_private.key

SERVER_PRIVATE=$(sudo cat /etc/wireguard/server_private.key)
SERVER_PUBLIC=$(sudo cat /etc/wireguard/server_public.key)

# Determine the default egress interface for NAT
DEFAULT_IF=$(ip route | grep '^default' | awk '{print $5}' | head -n1)

# Create WireGuard config
sudo tee /etc/wireguard/wg0.conf > /dev/null << EOF
[Interface]
PrivateKey = ${SERVER_PRIVATE}
Address = 10.100.0.1/24
ListenPort = 51820
SaveConfig = false

# Routing / NAT so tunnel traffic can reach the host
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o ${DEFAULT_IF} -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o ${DEFAULT_IF} -j MASQUERADE

# Peers are added dynamically by the platform (wg-manager) — do not edit manually.
EOF

sudo chmod 600 /etc/wireguard/wg0.conf

# Enable IP forwarding (needed for routing tunnel traffic)
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wireguard.conf
sudo sysctl -w net.ipv4.ip_forward=1

# Enable and start WireGuard
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0

echo ""
echo "============================================================"
echo "WireGuard server setup complete."
echo ""
echo "Add these to your .env:"
echo "  WG_SERVER_PUBLIC_KEY=${SERVER_PUBLIC}"
echo "  WG_SERVER_PRIVATE_KEY=${SERVER_PRIVATE}"
echo ""
echo "Open UDP port 51820 in the Google Cloud firewall:"
echo "  VPC Network -> Firewall -> Create Rule -> UDP:51820"
echo ""
echo "Verify with: sudo wg show"
echo "============================================================"
