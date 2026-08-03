FROM node:20-alpine

WORKDIR /app

RUN corepack enable

# Clone at build time, or COPY if you vendor the repo yourself
RUN apk add --no-cache git \
    && git clone --branch main --depth 1 https://github.com/developmentseed/stac-map.git . \
    && yarn install --frozen-lockfile

RUN apk add --no-cache python3
RUN yarn global add serve

EXPOSE 4173

# Build fresh on every container start so runtime env vars (VITE_*) take effect
CMD ["sh", "-c", "yarn build && npx serve -s dist -l 4173"]
