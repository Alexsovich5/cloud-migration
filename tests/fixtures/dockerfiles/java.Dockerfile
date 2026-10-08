FROM java:8u45-jre
MAINTAINER migration-framework
LABEL project="reports"

WORKDIR /app

COPY target/*.jar /app/app.jar

COPY . /app

EXPOSE 8080 8443

CMD ["java", "-jar", "app.jar"]
