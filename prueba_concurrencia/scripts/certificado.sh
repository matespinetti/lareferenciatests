#!/bin/sh
# CA autofirmada y certificado comodín *.sim.localhost para el servidor simulado.
# Uso: scripts/certificado.sh [directorio]   (por defecto: certs)
set -eu
DIR=${1:-certs}
mkdir -p "$DIR"
if [ -f "$DIR/servidor.pem" ] && [ -f "$DIR/ca.pem" ]; then
    exit 0
fi
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=Prueba concurrencia CA" \
    -keyout "$DIR/ca.key" -out "$DIR/ca.pem" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=*.sim.localhost" \
    -keyout "$DIR/servidor.key" -out "$DIR/servidor.csr" 2>/dev/null
cat > "$DIR/ext.cnf" <<EOF
subjectAltName=DNS:*.sim.localhost
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in "$DIR/servidor.csr" -CA "$DIR/ca.pem" -CAkey "$DIR/ca.key" -CAcreateserial \
    -days 365 -out "$DIR/servidor.pem" -extfile "$DIR/ext.cnf" 2>/dev/null
echo "Certificados en $DIR/"
