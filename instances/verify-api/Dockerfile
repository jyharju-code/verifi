FROM node:22-slim

WORKDIR /app
ENV NODE_ENV=production

COPY package.json ./
RUN npm install --omit=dev

COPY server.js ./
COPY routes/ routes/

CMD ["node", "server.js"]
