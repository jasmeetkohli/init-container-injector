# init-container-injector

Kubernetes Operator with KOPF framework (https://kopf.readthedocs.io/en/stable/)

This operator defines admission webhooks to validate a "dependency" resource and mutate pods if any dependency is found. 
Here's a better explanation : https://medium.com/@jasmeetkohlisingh/kubernetes-operator-with-kopf-23f86b593ff7
