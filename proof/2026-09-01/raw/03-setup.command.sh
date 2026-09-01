set -euo pipefail
uname -a
cat /etc/os-release
printf 'architecture='; uname -m
sha256sum /home/daytona/relay-hermes-source.tar.gz /home/daytona/relay-openapi.yaml
test "$(sha256sum /home/daytona/relay-hermes-source.tar.gz | cut -d' ' -f1)" = f21b327721124287517290ab9b7d2d8f6921d6152d60680dff1a03fd36997ece
test "$(sha256sum /home/daytona/relay-openapi.yaml | cut -d' ' -f1)" = f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5
mkdir -p /home/daytona/relay-hermes
tar -xzf /home/daytona/relay-hermes-source.tar.gz -C /home/daytona/relay-hermes
cd /home/daytona/relay-hermes
tar -tzf /home/daytona/relay-hermes-source.tar.gz | LC_ALL=C sort > /tmp/final-source-files.txt
while IFS= read -r file; do sha256sum "$file"; done < /tmp/final-source-files.txt > /tmp/final-source.sha256
sha256sum /tmp/final-source.sha256
test "$(sha256sum /tmp/final-source.sha256 | cut -d' ' -f1)" = a4107525372c4fccef190be5185bea91ab86972e39f7af5a479bd78e28e69306
git clone --filter=blob:none --no-checkout https://github.com/NousResearch/hermes-agent.git /home/daytona/hermes-agent
git -C /home/daytona/hermes-agent fetch --depth=1 origin 04224b2f82aabbe89525451089eb2677edfae179
git -C /home/daytona/hermes-agent checkout --detach 04224b2f82aabbe89525451089eb2677edfae179
test "$(git -C /home/daytona/hermes-agent rev-parse HEAD)" = 04224b2f82aabbe89525451089eb2677edfae179
uv python install 3.11 3.12 3.13
for py in 3.11 3.12 3.13; do
  uv venv --python "$py" ".venv-$py"
  uv pip install --python ".venv-$py/bin/python" -e ".[dev]" "build==1.3.0" "rich==15.0.0" "requests>=2"
  ".venv-$py/bin/python" --version
done
printf 'exact_source=pass files='; wc -l < /tmp/final-source-files.txt
