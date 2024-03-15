target base {
  dockerfile = "docker_build_files/base/Dockerfile"
  platforms = ["linux/arm64"]
  target = "base"
}

target slim_base {
  dockerfile = "docker_build_files/base/Dockerfile"
  platforms = ["linux/arm64"]
  target = "slim_base"
}

target host_base {
  dockerfile = "docker_build_files/base/Dockerfile"
  platforms = ["linux/arm64"]
  target = "host_base"
}

target python_base {
  dockerfile = "docker_build_files/base/Dockerfile"
  platforms = ["linux/arm64"]
  target = "python_base"
}

target wget_base {
  dockerfile = "docker_build_files/base/Dockerfile"
  platforms = ["linux/arm64"]
  target = "wget_base"
}

target rk {
  dockerfile = "docker_build_files/rockchip/Dockerfile"
  contexts = {
    base = "target:base",
    slim_base = "target:slim_base",
    host_base = "target:host_base",
    python_base = "target:python_base",
    wget_base = "target:wget_base"
  }
  platforms = ["linux/arm64"]
}