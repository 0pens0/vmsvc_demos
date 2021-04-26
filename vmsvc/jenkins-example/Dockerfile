FROM harbor-repo.vmware.com/dockerhub-proxy-cache/library/python:slim-buster

WORKDIR /

COPY requirements.txt /
RUN pip install -r /requirements.txt

COPY deploy_jenkins.py /

ENTRYPOINT ["python", "/deploy_jenkins.py"]
