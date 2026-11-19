// Bundles the plugin as an AMD module, the format Grafana loads. Everything Grafana already ships
// (React, @grafana/*, emotion, rxjs) is external, so it is shared at runtime and not bundled twice.
const path = require('path');
const CopyWebpackPlugin = require('copy-webpack-plugin');
const { EsbuildPlugin } = require('esbuild-loader');

const PLUGIN_ID = 'kekaflow-chat-app';

module.exports = {
  mode: 'production',
  devtool: false,
  context: __dirname,
  entry: { module: './src/module.ts' },
  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: '[name].js',
    library: { type: 'amd' },
    publicPath: `public/plugins/${PLUGIN_ID}/`,
    clean: true,
  },
  externals: [
    'lodash',
    'jquery',
    'moment',
    'slate',
    'emotion',
    '@emotion/react',
    '@emotion/css',
    'prismjs',
    'slate-plain-serializer',
    '@grafana/slate-react',
    'react',
    'react-dom',
    'react/jsx-runtime',
    'react-redux',
    'redux',
    'rxjs',
    'react-router',
    'react-router-dom',
    'd3',
    '@grafana/ui',
    '@grafana/runtime',
    '@grafana/data',
    '@grafana/i18n',
  ],
  resolve: { extensions: ['.ts', '.tsx', '.js'] },
  module: {
    rules: [
      {
        test: /\.[tj]sx?$/,
        exclude: /node_modules/,
        loader: 'esbuild-loader',
        options: { loader: 'tsx', target: 'es2022', jsx: 'automatic' },
      },
    ],
  },
  optimization: { minimizer: [new EsbuildPlugin({ target: 'es2022', legalComments: 'none' })] },
  performance: { hints: false },
  plugins: [
    new CopyWebpackPlugin({
      patterns: [
        { from: 'plugin.json', to: '.' },
        { from: 'src/img', to: 'img' },
      ],
    }),
  ],
};
