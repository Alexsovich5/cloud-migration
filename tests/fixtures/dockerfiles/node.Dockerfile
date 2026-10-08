FROM node:0.12-slim
MAINTAINER migration-framework
LABEL project="api-gateway"

WORKDIR /app

COPY package.json /app/
RUN npm install --production

COPY . /app

EXPOSE 3000

CMD ["node", "server.js"]
