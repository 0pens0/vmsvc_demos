FROM harbor-repo.vmware.com/dockerhub-proxy-cache/library/python:3.7-slim-buster

WORKDIR /

RUN mkdir /data
VOLUME /data

COPY requirements.txt /
RUN pip install -r /requirements.txt

COPY cloud-config.yaml /
COPY deploy-cloud-init.py /

ENTRYPOINT ["python", "/deploy-cloud-init.py", "--cloud-config", "/cloud-config.yaml"]
