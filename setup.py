from setuptools import setup, find_packages


def readme():
    # Явная кодировка: без неё на Windows файл читается в локальной кодировке и сборка
    # падает на любом не-ASCII символе в README.
    with open('README.md', 'r', encoding='utf-8') as f:
        return f.read()


setup(
    name='proxy_seller_user_api',
    # 2.x = Client API v2 (baseUrl .../personal/api/v2/, ObjectId-строки, конверт
    # {status, data, errors}). 2.1 добавляет balance/autotopup/get и /set.
    version='2.1.0',
    author='proxy-seller',
    author_email='support@proxy-seller.com',
    description='Client library for the proxy-seller.com Client API v2',
    long_description=readme(),
    long_description_content_type='text/markdown',
    url='https://github.com/proxy-seller/user-api-python',
    license='MIT',
    # tests — служебный пакет офлайн-проверок, в дистрибутив он не нужен.
    packages=find_packages(exclude=['tests', 'tests.*']),
    install_requires=['requests>=2.31.0'],
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Topic :: Internet :: Proxy Servers',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ],
    keywords='proxy-seller proxy api client rest residential',
    project_urls={
        'Documentation': 'https://proxy-seller.com/personal/api/',
        'Source': 'https://github.com/proxy-seller/user-api-python',
        'Issues': 'https://github.com/proxy-seller/user-api-python/issues'
    },
    python_requires='>=3.7'
)
