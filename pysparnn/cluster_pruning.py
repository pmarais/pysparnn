# Copyright (c) 2016-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
"""Defines a cluster pruing search structure to do sparse K-NN Queries"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
import collections
import random
import numpy as np
from scipy.sparse import vstack
import pysparnn.matrix_distance

def k_best(tuple_list, k):
    """For a list of tuples [(distance, value), ...] - Get the k-best tuples by 
    distance.
    Args:
        tuple_list: List of tuples. (distance, value)
        k: Number of tuples to return.
    """
    tuple_lst = sorted(tuple_list, key=lambda x: x[0],
                       reverse=False)[:k]

    return tuple_lst

def filter_unique(tuple_list):
    """For a list of tuples [(distance, value), ...] - filter out duplicate 
    values.
    Args:
        tuple_list: List of tuples. (distance, value)
    """

    added = set()
    ret = []
    for distance, value in tuple_list:
        if not value in added:
            ret.append((distance, value))
            added.add(value)
    return ret


def filter_distance(results, return_distance):
    """For a list of tuples [(distance, value), ...] - optionally filter out 
    the distance elements.
    Args:
        tuple_list: List of tuples. (distance, value)
        return_distance: boolean to determine if distances should be returned. 
    """
    if return_distance:
        return results
    else:
        return list([x for y, x in results])


class ClusterIndex(object):
    """Search structure which gives speedup at slight loss of recall.

       Uses cluster pruning structure as defined in:
       http://nlp.stanford.edu/IR-book/html/htmledition/cluster-pruning-1.html

       tldr - searching for a document in an index of K documents is naievely
           O(K). However you can create a tree structure where the first level
           is O(sqrt(K)) and each of the leaves are also O(sqrt(K)).

           You randomly pick sqrt(K) items to be in the top level. Then for
           the K doccuments you assign it to the closest neighbor in the top
           level.

           This breaks up one O(K) search into O(2 * sqrt(K)) searches which
           is much much faster when K is big.

           This generalizes to h levels. The runtime becomes:
               O(h * h_root(K))
    """

    def __init__(self, sparse_features, records_data,
                 distance_type=pysparnn.matrix_distance.CosineDistance,
                 matrix_size=None,
                 parent=None):
        """Create a search index composed of recursively defined sparse
        matricies. Does recursive KNN search. See class docstring for a 
        description of the method.

        Args:
            sparse_features: A csr_matrix with rows that represent records
                (corresponding to the elements in records_data) and columns
                that describe a point in space for each row.
            records_data: Data to return when a doc is matched. Index of
                corresponds to records_features.
            distance_type: Class that defines the distance measure to use.
            matrix_size: Ideal size for matrix multiplication. This controls
                the depth of the tree. Defaults to 2 levels (approx). Highly
                reccomended that the default value is used.
        """

        self.is_terminal = False
        self.parent = parent
        self.distance_type = distance_type
        self.desired_matrix_size = matrix_size
        num_records = sparse_features.shape[0]

        if matrix_size is None:
            matrix_size = max(int(np.sqrt(num_records)), 100)
        else:
            matrix_size = int(matrix_size)

        self.matrix_size = matrix_size

        num_levels = np.log(num_records)/np.log(self.matrix_size)

        if num_levels <= 1.4:
            self.is_terminal = True
            self.root = distance_type(sparse_features,
                                      records_data)
        else:
            self.is_terminal = False
            records_data = np.array(records_data)

            records_index = np.arange(sparse_features.shape[0])
            clusters_size = min(self.matrix_size, num_records)
            clusters_selection = random.sample(records_index, clusters_size)
            clusters_selection = sparse_features[clusters_selection]

            item_to_clusters = collections.defaultdict(list)

            root = distance_type(clusters_selection,
                                 np.arange(clusters_selection.shape[0]))

            rng_step = self.matrix_size
            for rng in range(0, sparse_features.shape[0], rng_step):
                max_rng = min(rng + rng_step, sparse_features.shape[0])
                records_rng = sparse_features[rng:max_rng]
                for i, clstrs in enumerate(root.nearest_search(records_rng, k=1)):
                    for _, cluster in clstrs:
                        item_to_clusters[cluster].append(i + rng)

            clusters = []
            cluster_keeps = []
            for k, clust_sel in enumerate(clusters_selection):
                clustr = item_to_clusters[k]
                if len(clustr) > 0:
                    index = ClusterIndex(vstack(sparse_features[clustr]),
                                         records_data[clustr],
                                         distance_type=distance_type,
                                         matrix_size=self.matrix_size, 
                                         parent=self)
                    clusters.append(index)
                    cluster_keeps.append(clust_sel)

            cluster_keeps = vstack(cluster_keeps)
            clusters = np.array(clusters)

            self.root = distance_type(cluster_keeps, clusters)


    def insert(self, sparse_feature, record):
        """Insert a single record into the index.
        
        Args:
            sparse_feature: sparse feature vector
            record: record to return as the result of a search
        """
        
        nearest = self
        while not nearest.is_terminal:
            nearest = nearest.root.nearest_search(sparse_feature, k=1)
            _, nearest = nearest[0][0]

        cluster_index = nearest
        parent_index = cluster_index.parent
        while parent_index and cluster_index.matrix_size * 2 < \
                len(cluster_index.root.get_records()):
            cluster_index = parent_index
            parent_index = cluster_index.parent
       
        cluster_index._reindex(sparse_feature, record)

        

    def _get_child_data(self):
        """Get all of the features and corresponding records represented in the
        full tree structure.
        
        Returns:
            A tuple of (list(features), list(records)). 
        """

        if self.is_terminal:
            return [self.root.get_feature_matrix()], [self.root.get_records()]
        else:
            result_features = []
            result_records = []
    
            for c in self.root.get_records():
                features, records = c._get_child_data()

                result_features.extend(features)
                result_records.extend(records)
    
            return result_features, result_records 
    
    def _reindex(self, sparse_feature=None, record=None):
        """Rebuild the search index. Optionally add a record. This is used
        when inserting records to the index.
        
        Args:
            sparse_feature: sparse feature vector
            record: record to return as the result of a search
        """

        features, records = self._get_child_data()

        flat_rec = []
        for x in records:
            flat_rec.extend(x)

        if sparse_feature <> None and record <> None:
            features.append(sparse_feature)
            flat_rec.append(record)

        self.__init__(vstack(features), flat_rec, self.distance_type, 
                self.desired_matrix_size, self.parent)


    def _search(self, sparse_features, k=1, 
                max_distance=None, k_clusters=1):
        """Find the closest item(s) for each feature_list in.

        Args:
            sparse_features: A csr_matrix with rows that represent records
                (corresponding to the elements in records_data) and columns
                that describe a point in space for each row.
            k: Return the k closest results.
            max_distance: Return items no more than max_distance away from the
                query point. Defaults to any distance.
            k_clusters: number of branches (clusters) to search at each level.
                This increases recall at the cost of some speed.

                Note: max_distance constraints are also applied.
                    This means there may be less than k_clusters searched at
                    each level. 
                    This means each search will fully traverse at least one
                    (but at most k_clusters) clusters at each level.

        Returns:
            For each element in features_list, return the k-nearest items
            and their distance score
            [[(score1_1, item1_1), ..., (score1_k, item1_k)],
             [(score2_1, item2_1), ..., (score2_k, item2_k)], ...]
        """
        if self.is_terminal:
            return self.root.nearest_search(sparse_features, k=k,
                                            max_distance=max_distance)
        else:
            ret = []
            nearest = self.root.nearest_search(sparse_features, k=k_clusters)

            for i, nearest_clusters in enumerate(nearest):
                curr_ret = []
                for distance, cluster in nearest_clusters:

                    cluster_items = cluster.\
                            search(sparse_features[i], k=k,
                                   k_clusters=k_clusters,
                                   max_distance=max_distance)

                    for elements in cluster_items:
                        if len(elements) > 0:
                            curr_ret.extend(elements)
                ret.append(k_best(curr_ret, k))
            return ret

    def search(self, sparse_features, k=1, max_distance=None, k_clusters=1, 
            return_distance=True):
        """Find the closest item(s) for each feature_list in the index.

        Args:
            sparse_features: A csr_matrix with rows that represent records
                (corresponding to the elements in records_data) and columns
                that describe a point in space for each row.
            k: Return the k closest results.
            max_distance: Return items no more than max_distance away from the
                query point. Defaults to any distance.
            k_clusters: number of branches (clusters) to search at each level.
                This increases recall at the cost of some speed.

                Note: max_distance constraints are also applied.
                    This means there may be less than k_clusters searched at
                    each level. 

                    This means each search will fully traverse at least one
                    (but at most k_clusters) clusters at each level.

        Returns:
            For each element in features_list, return the k-nearest items
            and (optionally) their distance score
            [[(score1_1, item1_1), ..., (score1_k, item1_k)],
             [(score2_1, item2_1), ..., (score2_k, item2_k)], ...]

            Note: if return_distance == False then the scores are omitted
            [[item1_1, ..., item1_k],
             [item2_1, ..., item2_k], ...]
        """
        
        # search no more than 1k records at once
        # helps keap the matrix multiplies small
        batch_size = 1000
        results = []
        rng_step = batch_size
        for rng in range(0, sparse_features.shape[0], rng_step):
            max_rng = min(rng + rng_step, sparse_features.shape[0])
            records_rng = sparse_features[rng:max_rng]

            results.extend(self._search(sparse_features=records_rng,
                                        k=k,
                                        max_distance=max_distance,
                                        k_clusters=k_clusters))

        return [filter_distance(res, return_distance) for res in results]
        
    def _print_structure(self, tabs=''):
        """Pretty print the tree index structure's matrix sizes"""
        print(tabs + str(self.root.matrix.shape[0]))
        if not self.is_terminal:
            for index in self.root.records_data:
                index.print_structure(tabs + '  ')

    def _max_depth(self):
        """Yield the max depth of the tree index"""
        if not self.is_terminal:
            max_dep = 0
            for index in self.root.records_data:
                max_dep = max(max_dep, index._max_depth())
            return 1 + max_dep
        else:
            return 1

    def _matrix_sizes(self, ret=None):
        """Return all of the matrix sizes within the index"""
        if ret is None:
            ret = []
        ret.append(len(self.root.records_data))
        if not self.is_terminal:
            for index in self.root.records_data:
                ret.extend(index._matrix_sizes())
        return ret


class MultiClusterIndex(object):    
    """Search structure which provides query speedup at the loss of recall.

       There are two components to this.

       = Cluster Indexes =
       Uses cluster pruning index structure as defined in:
       http://nlp.stanford.edu/IR-book/html/htmledition/cluster-pruning-1.html

       Refer to ClusterIndex documentation. 

       = Multiple Indexes =
       The MultiClusterIndex creates multiple ClusterIndexes. This method 
       gives better recall at the cost of allocating more memory. The 
       ClusterIndexes are created by randomly picking representative clusters.
       The randomization tends to do a pretty good job but it is not perfect.
       Elements can be assigned to clusters that are far from an optimal match.
       Creating more Indexes (random cluster allocations) increases the chances 
       of finding a good match.

       There are three perameters that impact recall. Will discuss them all 
       here:
       1) MuitiClusterIndex(matrix_size) 
           This impacts the tree structure (see cluster index documentation). 
           Has a good default value. By increasing this value your index will
           behave increasingly like brute force search and you will loose query
           efficiency. If matrix_size is greater than your number of records 
           you get brute force search.
       2) MuitiClusterIndex.search(k_clusters) 
           Number of clusters to check when looking for records. This increases
           recall at the cost of query speed. Can be specified dynamically.
       3) MuitiClusterIndex(num_indexes) 
           Number of indexes to generate. This increases recall at the cost of 
           query speed. It also increases memory usage. It can only be 
           specified at index construction time. 
           
           Compared to (2) this argument gives better recall and has comparable 
           speed. This statement assumes default (automatic) matrix_size is 
           used.
            Scenario 1:

            (a) num_indexes=2, k_clusters=1
            (b) num_indexes=1, k_clusters=2

            (a) will have better recall but consume 2x the memory. (a) will be
            slightly slower than (b).

            Scenario 2:

            (a) num_indexes=2, k_clusters=1, matrix_size >> records
            (b) num_indexes=1, k_clusters=2, matrix_size >> records

            This means that each index does a brute force search. (a) and (b) 
            will have the same recall. (a) will be 2x slower than (b). (a) will
            consume 2x the memory of (b).

            Scenario 1 will be much faster than Scenario 2 for large data. 
            Scenario 2 will have better recall than Scenario 1. 
    """

    def __init__(self, sparse_features, records_data,
                 distance_type=pysparnn.matrix_distance.CosineDistance,
                 matrix_size=None, num_indexes=2):
        """Create a search index composed of multtiple ClusterIndexes. See 
        class docstring for a description of the method.

        Args:
            sparse_features: A csr_matrix with rows that represent records
                (corresponding to the elements in records_data) and columns
                that describe a point in space for each row.
            records_data: Data to return when a doc is matched. Index of
                corresponds to records_features.
            distance_type: Class that defines the distance measure to use.
            matrix_size: Ideal size for matrix multiplication. This controls
                the depth of the tree. Defaults to 2 levels (approx). Highly
                reccomended that the default value is used.
            num_indexes: Number of ClusterIndexes to construct. Improves recall
                at the cost of memory.
        """


        self.indexes = []
        for _ in range(num_indexes):
            self.indexes.append((ClusterIndex(sparse_features, records_data,
                                              distance_type, matrix_size)))

    def insert(self, sparse_feature, record):
        """Insert a single record into the index.
        
        Args:
            sparse_feature: sparse feature vector
            record: record to return as the result of a search
        """
        for ind in self.indexes:
            ind.insert(sparse_feature, record)

    def search(self, sparse_features, k=1, max_distance=None, k_clusters=1, 
               return_distance=True, num_indexes=None):
        """Find the closest item(s) for each feature_list in the index.

        Args:
            sparse_features: A csr_matrix with rows that represent records
                (corresponding to the elements in records_data) and columns
                that describe a point in space for each row.
            k: Return the k closest results.
            max_distance: Return items no more than max_distance away from the
                query point. Defaults to any distance.
            k_clusters: number of branches (clusters) to search at each level
                within each index. This increases recall at the cost of some 
                speed.

                Note: max_distance constraints are also applied.
                    This means there may be less than k_clusters searched at
                    each level. 

                    This means each search will fully traverse at least one
                    (but at most k_clusters) clusters at each level.
            num_indexes: number of indexes to search. This increases recall at
                the cost of some speed. Can not be larger than the number of 
                num_indexes that was specified in the constructor. Defaults to
                searching all indexes.

        Returns:
            For each element in features_list, return the k-nearest items
            and (optionally) their distance score
            [[(score1_1, item1_1), ..., (score1_k, item1_k)],
             [(score2_1, item2_1), ..., (score2_k, item2_k)], ...]

            Note: if return_distance == False then the scores are omitted
            [[item1_1, ..., item1_k],
             [item2_1, ..., item2_k], ...]
        """
        results = []
        if num_indexes is None:
            num_indexes = len(self.indexes)
        for ind in self.indexes[:num_indexes]:
            results.append(ind.search(sparse_features, k, max_distance,
                                      k_clusters, True))
        ret = []
        for r in np.hstack(results):
            ret.append(
                filter_distance(
                    k_best(filter_unique(r), k), 
                    return_distance
                )
            )

        return ret 
