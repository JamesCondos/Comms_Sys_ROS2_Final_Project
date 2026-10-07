from setuptools import setup

setup(
    name="rest_traffic", version="0.1.0", packages=["rest_traffic"],
    data_files=[("share/ament_index/resource_index/packages", ["resource/rest_traffic"]),
                ("share/rest_traffic", ["package.xml"])],
    install_requires=["setuptools"], zip_safe=True,
    maintainer="Project author", maintainer_email="student@example.invalid",
    description="Namespaced experimental ROS2 traffic", license="UNLICENSED",
    entry_points={"console_scripts": ["traffic_sources = rest_traffic.generator:main"]},
)
