# The collector is a single stdlib-only module, so there is nothing to install
# and no dependency layer to cache. Alpine keeps the image around 50 MB.
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="govee-extract" \
      org.opencontainers.image.description="Read CO2, temperature and humidity from a Govee H5140 into InfluxDB" \
      org.opencontainers.image.source="https://github.com/vzaliva/govee-extract"

COPY govee_h5140.py /usr/local/bin/govee-h5140
# A real home directory, so the default ~/.config/govee/api_key lookup works
# when the key is supplied as a mounted file rather than an env var.
RUN chmod +x /usr/local/bin/govee-h5140 \
 && adduser -D -u 1000 govee

USER govee

# Long-running poll by default. The API quota is 10000 requests/account/day, so
# intervals shorter than ~10s will exhaust it; 300s is a comfortable default.
ENTRYPOINT ["govee-h5140"]
CMD ["read", "--watch", "300", "--quiet"]
