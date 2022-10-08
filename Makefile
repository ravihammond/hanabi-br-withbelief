default:
	@make -s build-cpp

build-cpp:
	sh scripts/build_cpp.sh

build-cudaconda: 
	docker build -t ravihammond/cudaconda -f dockerfiles/Dockerfile.cudaconda .

build-dev: 
	docker build -t ravihammond/hanabi-project:dev -f dockerfiles/Dockerfile.project --target dev .

build-prod:
	docker build -t ravihammond/hanabi-project:prod -f dockerfiles/Dockerfile.project --target prod .

run-dev:
	bash scripts/run_docker_dev.bash

run-prod:
	bash scripts/run_docker_prod.bash

