FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
# Vendored TradingView Lightweight Charts (no client-side CDN dependency) for the SEL price chart.
COPY lightweight-charts.standalone.production.js /usr/share/nginx/html/lightweight-charts.standalone.production.js
