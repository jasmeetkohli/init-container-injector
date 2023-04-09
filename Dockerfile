FROM python:3.11
ADD . /opt/dependency-injector
RUN pip install kopf[dev]
RUN pip install kubernetes
RUN pip install pykube-ng
RUN apt-get update
RUN apt-get install -y vim dnsutils lsof
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
 && mv kubectl /usr/local/bin/kubectl \
 && chmod +x /usr/local/bin/kubectl
ENTRYPOINT kubectl proxy --accept-hosts '.*' --port=80 & kopf run /opt/dependency-injector/src/dependency_manager.py
