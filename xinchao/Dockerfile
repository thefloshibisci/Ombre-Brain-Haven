FROM node:20-slim

WORKDIR /app

COPY package.json package.json
COPY configs configs
COPY src src

ENV PORT=18110

EXPOSE 18110

CMD ["node", "src/server.js"]
