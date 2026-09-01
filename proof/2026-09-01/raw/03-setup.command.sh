set -euo pipefail
uname -a
cat /etc/os-release
printf 'architecture='; uname -m
sha256sum /home/daytona/relay-hermes-source.tar.gz /home/daytona/relay-openapi.yaml
test "$(sha256sum /home/daytona/relay-hermes-source.tar.gz | cut -d' ' -f1)" = 6e90826169291e53fb875998f79196520877216419644dac0f61eebfe3b00478
test "$(sha256sum /home/daytona/relay-openapi.yaml | cut -d' ' -f1)" = f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5
mkdir -p /home/daytona/relay-hermes
tar -xzf /home/daytona/relay-hermes-source.tar.gz -C /home/daytona/relay-hermes
git clone --filter=blob:none --no-checkout https://github.com/NousResearch/hermes-agent.git /home/daytona/hermes-agent
git -C /home/daytona/hermes-agent fetch --depth=1 origin 04224b2f82aabbe89525451089eb2677edfae179
git -C /home/daytona/hermes-agent checkout --detach 04224b2f82aabbe89525451089eb2677edfae179
test "$(git -C /home/daytona/hermes-agent rev-parse HEAD)" = 04224b2f82aabbe89525451089eb2677edfae179
uv python install 3.11 3.12 3.13
for py in 3.11 3.12 3.13; do
  uv venv --python "$py" "/home/daytona/relay-hermes/.venv-$py"
  uv pip install --python "/home/daytona/relay-hermes/.venv-$py/bin/python" -e "/home/daytona/relay-hermes[dev]" "build==1.3.0" "rich==15.0.0"
  "/home/daytona/relay-hermes/.venv-$py/bin/python" --version
done
