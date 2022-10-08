#!/usr/bin/env bash

docker run --rm -it \
    --volume=$(pwd):/app/:rw \
    --gpus all \
    --ipc host \
    --env WANDB_TOKEN=$(cat keys/wandb_api_key.txt) \
    ravihammond/hanabi-project:dev \
    ${@:-bash}


