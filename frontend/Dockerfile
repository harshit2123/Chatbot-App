FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5173

# Dev server is intentional for this take-home: it keeps VITE_API_URL runtime
# configurable via compose. A production image would `npm run build` and serve
# the static bundle behind nginx.
CMD ["npm", "run", "dev"]
