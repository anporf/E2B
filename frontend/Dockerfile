FROM node:18-alpine as build
WORKDIR /app
COPY package.json .
RUN apk add --no-cache git
RUN npm install
COPY . .
# RUN npm run build - Нужно отдельно запустиь команду из frontend

FROM node:18-alpine
WORKDIR /app
RUN apk add --no-cache git
COPY --from=build /app/dist ./dist
COPY package.json .
RUN npm install --omit=dev
EXPOSE 4000
CMD ["node", "dist/server.js"]
