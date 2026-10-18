FROM node:22.14.0-alpine3.21 AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite embeds these values into the static bundle during this build stage.
# Firebase web configuration is public client configuration; never pass a
# service-account key or other private credential as a VITE_* value.
ARG VITE_API_URL=/api
ARG VITE_FIREBASE_API_KEY=
ARG VITE_FIREBASE_AUTH_DOMAIN=
ARG VITE_FIREBASE_PROJECT_ID=
ARG VITE_FIREBASE_APP_ID=
ARG VITE_USE_FIREBASE_EMULATORS=false
ARG VITE_FIREBASE_AUTH_EMULATOR_URL=
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_FIREBASE_API_KEY=${VITE_FIREBASE_API_KEY} \
    VITE_FIREBASE_AUTH_DOMAIN=${VITE_FIREBASE_AUTH_DOMAIN} \
    VITE_FIREBASE_PROJECT_ID=${VITE_FIREBASE_PROJECT_ID} \
    VITE_FIREBASE_APP_ID=${VITE_FIREBASE_APP_ID} \
    VITE_USE_FIREBASE_EMULATORS=${VITE_USE_FIREBASE_EMULATORS} \
    VITE_FIREBASE_AUTH_EMULATOR_URL=${VITE_FIREBASE_AUTH_EMULATOR_URL}

RUN npm run build

FROM nginx:1.27.4-alpine3.21

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
