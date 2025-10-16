#!/bin/bash

# # Create a health check script
# RUN echo '#!/bin/bash\npgrep -f "python main.py" > /dev/null || exit 1' > /usr/local/bin/healthcheck.sh \
#     && chmod +x /usr/local/bin/healthcheck.sh
# docker inspect --format='{{json .State.Health}}' my-running-app

# Check if the python main.py process is running
if pgrep -f "python main.py" > /dev/null; then
    exit 0  # Process is running, health check passed
else
    exit 1  # Process is not running, health check failed
fi
