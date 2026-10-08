FROM php:5.6-apache
MAINTAINER migration-framework
LABEL project="legacy-crm"

COPY . /var/www/html/

EXPOSE 80
