from glob import glob
from setuptools import find_packages, setup

package_name = 'complex_network'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Project team',
    maintainer_email='student@example.com',
    description='Bare-bones two-site ROS 2 network.',
    license='UNLICENSED',
    entry_points={'console_scripts': [
        'robot = complex_network.nodes:robot_main',
        'relay = complex_network.nodes:relay_main',
        'load_balancer = complex_network.nodes:balancer_main',
        'worker = complex_network.nodes:worker_main',
        'video = complex_network.video:main',
    ]},
)
